from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2015 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six
import numpy as np
import loopy as lp
import pyopencl as cl
import pyopencl.array  # noqa
from pytools import memoize_in

import grudge.symbolic.mappers as mappers
from grudge import sym

import logging
logger = logging.getLogger(__name__)


# {{{ exec mapper

class ExecutionMapper(mappers.Evaluator,
        mappers.BoundOpMapperMixin,
        mappers.LocalOpReducerMixin):
    def __init__(self, queue, context, bound_op):
        super(ExecutionMapper, self).__init__(context)
        self.discrwb = bound_op.discrwb
        self.bound_op = bound_op
        self.queue = queue

    # {{{ expression mappings -------------------------------------------------

    def map_ones(self, expr):
        if expr.dd.is_scalar():
            return 1

        discr = self.discrwb.discr_from_dd(expr.dd)

        result = discr.empty(self.queue, allocator=self.bound_op.allocator)
        result.fill(1)
        return result

    def map_node_coordinate_component(self, expr):
        discr = self.discrwb.discr_from_dd(expr.dd)
        return discr.nodes()[expr.axis].with_queue(self.queue)

    def map_grudge_variable(self, expr):
        from numbers import Number

        value = self.context[expr.name]
        if not expr.dd.is_scalar() and isinstance(value, Number):
            discr = self.discrwb.discr_from_dd(expr.dd)
            ary = discr.empty(self.queue)
            ary.fill(value)
            value = ary

        return value

    def map_subscript(self, expr):
        value = super(ExecutionMapper, self).map_subscript(expr)

        if isinstance(expr.aggregate, sym.Variable):
            dd = expr.aggregate.dd

            from numbers import Number
            if not dd.is_scalar() and isinstance(value, Number):
                discr = self.discrwb.discr_from_dd(dd)
                ary = discr.empty(self.queue)
                ary.fill(value)
                value = ary
        return value

    def map_call(self, expr):
        from pymbolic.primitives import Variable
        assert isinstance(expr.function, Variable)

        # FIXME: Make a way to register functions

        args = [self.rec(p) for p in expr.parameters]
        from numbers import Number
        representative_arg = args[0]
        if (
                isinstance(representative_arg,  Number)
                or (isinstance(representative_arg, np.ndarray)
                    and representative_arg.shape == ())):
            func = getattr(np, expr.function.name)
            return func(*args)

        cached_name = "map_call_knl_"

        i = Variable("i")
        func = Variable(expr.function.name)
        if expr.function.name == "fabs":  # FIXME
            func = Variable("abs")
            cached_name += "abs"
        else:
            cached_name += expr.function.name

        @memoize_in(self.bound_op, cached_name)
        def knl():
            knl = lp.make_kernel(
                "{[i]: 0<=i<n}",
                [
                    lp.Assignment(Variable("out")[i],
                        func(Variable("a")[i]))
                ], default_offset=lp.auto)
            return lp.split_iname(knl, "i", 128, outer_tag="g.0", inner_tag="l.0")

        assert len(args) == 1
        evt, (out,) = knl()(self.queue, a=args[0])

        return out

    def map_nodal_sum(self, op, field_expr):
        # FIXME: Could allow array scalars
        return cl.array.sum(self.rec(field_expr)).get()[()]

    def map_nodal_max(self, op, field_expr):
        # FIXME: Could allow array scalars
        return cl.array.max(self.rec(field_expr)).get()[()]

    def map_nodal_min(self, op, field_expr):
        # FIXME: Could allow array scalars
        return cl.array.min(self.rec(field_expr)).get()[()]

    def map_if(self, expr):
        bool_crit = self.rec(expr.condition)

        then = self.rec(expr.then)
        else_ = self.rec(expr.else_)

        import pymbolic.primitives as p
        var = p.Variable

        i = var("i")
        if isinstance(then,  pyopencl.array.Array):
            sym_then = var("a")[i]
        elif isinstance(then,  np.number):
            sym_then = var("a")
        else:
            raise TypeError(
                "Expected parameter to be of type np.number or pyopencl.array.Array")

        if isinstance(else_,  pyopencl.array.Array):
            sym_else = var("b")[i]
        elif isinstance(then,  np.number):
            sym_else = var("b")
        else:
            raise TypeError(
                "Expected parameter to be of type np.number or pyopencl.array.Array")

        @memoize_in(self.bound_op, "map_if_knl")
        def knl():
            knl = lp.make_kernel(
                "{[i]: 0<=i<n}",
                [
                    lp.Assignment(var("out")[i],
                        p.If(var("crit")[i], sym_then, sym_else))
                ])
            return lp.split_iname(knl, "i", 128, outer_tag="g.0", inner_tag="l.0")

        evt, (out,) = knl()(self.queue, crit=bool_crit, a=then, b=else_)

        return out

    def map_ref_diff_base(self, op, field_expr):
        raise NotImplementedError(
                "differentiation should be happening in batched form")

    def map_elementwise_linear(self, op, field_expr):
        field = self.rec(field_expr)

        from grudge.tools import is_zero
        if is_zero(field):
            return 0

        @memoize_in(self.bound_op, "elwise_linear_knl")
        def knl():
            knl = lp.make_kernel(
                """{[k,i,j]:
                    0<=k<nelements and
                    0<=i,j<ndiscr_nodes}""",
                "result[k,i] = sum(j, mat[i, j] * vec[k, j])",
                default_offset=lp.auto, name="diff")

            knl = lp.split_iname(knl, "i", 16, inner_tag="l.0")
            return lp.tag_inames(knl, dict(k="g.0"))

        discr = self.discrwb.discr_from_dd(op.dd_in)

        # FIXME: This shouldn't really assume that it's dealing with a volume
        # input. What about quadrature? What about boundaries?
        result = discr.empty(
                queue=self.queue,
                dtype=field.dtype, allocator=self.bound_op.allocator)

        for grp in discr.groups:
            cache_key = "elwise_linear", grp, op, field.dtype
            try:
                matrix = self.bound_op.operator_data_cache[cache_key]
            except KeyError:
                matrix = (
                        cl.array.to_device(
                            self.queue,
                            np.asarray(op.matrix(grp), dtype=field.dtype))
                        .with_queue(None))

                self.bound_op.operator_data_cache[cache_key] = matrix

            knl()(self.queue, mat=matrix, result=grp.view(result),
                    vec=grp.view(field))

        return result

    def map_elementwise_max(self, op, field_expr):
        from grudge._internal import perform_elwise_max
        field = self.rec(field_expr)

        out = self.discr.volume_zeros(dtype=field.dtype)
        for eg in self.discr.element_groups:
            perform_elwise_max(eg.ranges, field, out)

        return out

    def map_interpolation(self, op, field_expr):
        conn = self.discrwb.connection_from_dds(op.dd_in, op.dd_out)
        return conn(self.queue, self.rec(field_expr)).with_queue(self.queue)

    def map_opposite_partition_face_swap(self, op, field_expr):
        from mpi4py import MPI
        mpi_comm = MPI.COMM_WORLD

        grp_factory = self.discrwb.group_factory_for_quadrature_tag(sym.QTAG_NONE)

        volume_discr = self.discrwb.discr_from_dd("vol")
        from meshmode.distributed import MPIBoundaryCommunicator
        bdry_conn_future = MPIBoundaryCommunicator(mpi_comm, self.queue,
                                                   volume_discr,
                                                   grp_factory,
                                                   op.i_remote_part)
        # TODO: Need to tell the future what boundary data to transfer
        bdry_conn, _ = bdry_conn_future()
        return bdry_conn(self.queue, self.rec(field_expr)).with_queue(self.queue)

    def map_opposite_interior_face_swap(self, op, field_expr):
        dd = op.dd_in

        qtag = dd.quadrature_tag
        if qtag is None:
            # FIXME: Remove once proper quadrature support arrives
            qtag = sym.QTAG_NONE

        return self.discrwb.opposite_face_connection(qtag)(
                self.queue, self.rec(field_expr)).with_queue(self.queue)

    # {{{ face mass operator

    def map_ref_face_mass_operator(self, op, field_expr):
        field = self.rec(field_expr)

        from grudge.tools import is_zero
        if is_zero(field):
            return 0

        @memoize_in(self.bound_op, "face_mass_knl")
        def knl():
            knl = lp.make_kernel(
                """{[k,i,f,j]:
                    0<=k<nelements and
                    0<=f<nfaces and
                    0<=i<nvol_nodes and
                    0<=j<nface_nodes}""",
                "result[k,i] = sum(f, sum(j, mat[i, f, j] * vec[f, k, j]))",
                default_offset=lp.auto, name="face_mass")

            knl = lp.split_iname(knl, "i", 16, inner_tag="l.0")
            return lp.tag_inames(knl, dict(k="g.0"))

        all_faces_conn = self.discrwb.connection_from_dds("vol", op.dd_in)
        all_faces_discr = all_faces_conn.to_discr
        vol_discr = all_faces_conn.from_discr

        result = vol_discr.empty(
                queue=self.queue,
                dtype=field.dtype, allocator=self.bound_op.allocator)

        assert len(all_faces_discr.groups) == len(vol_discr.groups)

        for cgrp, afgrp, volgrp in zip(all_faces_conn.groups,
                all_faces_discr.groups, vol_discr.groups):
            cache_key = "face_mass", afgrp, op, field.dtype

            nfaces = volgrp.mesh_el_group.nfaces

            try:
                matrix = self.bound_op.operator_data_cache[cache_key]
            except KeyError:
                matrix = op.matrix(afgrp, volgrp, field.dtype)
                matrix = (
                        cl.array.to_device(self.queue, matrix)
                        .with_queue(None))

                self.bound_op.operator_data_cache[cache_key] = matrix

            input_view = afgrp.view(field).reshape(
                    nfaces, volgrp.nelements, afgrp.nunit_nodes)
            knl()(self.queue, mat=matrix, result=volgrp.view(result),
                    vec=input_view)

        return result

    # }}}

    # }}}

    # {{{ code execution functions
    def map_insn_loopy_kernel(self, insn):
        kwargs = {}
        kdescr = insn.kernel_descriptor
        for name, expr in six.iteritems(kdescr.input_mappings):
            kwargs[name] = self.rec(expr)

        discr = self.discrwb.discr_from_dd(kdescr.governing_dd)
        for name in kdescr.scalar_args():
            v = kwargs[name]
            if isinstance(v, (int, float)):
                kwargs[name] = discr.real_dtype.type(v)
            elif isinstance(v, complex):
                kwargs[name] = discr.complex_dtype.type(v)
            elif isinstance(v, np.number):
                pass
            else:
                raise ValueError("unrecognized scalar type for variable '%s': %s"
                        % (name, type(v)))

        kwargs["grdg_n"] = discr.nnodes
        evt, result_dict = kdescr.loopy_kernel(self.queue, **kwargs)
        return list(result_dict.items()), []

    def map_insn_assign(self, insn):
        return [(name, self.rec(expr))
                for name, expr in zip(insn.names, insn.exprs)], []

    def map_insn_assign_to_discr_scoped(self, insn):
        assignments = []
        for name, expr in zip(insn.names, insn.exprs):
            value = self.rec(expr)
            self.discrwb._discr_scoped_subexpr_name_to_value[name] = value
            assignments.append((name, value))

        return assignments, []

    def map_insn_assign_from_discr_scoped(self, insn):
        return [(insn.name,
            self.discrwb._discr_scoped_subexpr_name_to_value[insn.name])], []

    def map_insn_diff_batch_assign(self, insn):
        field = self.rec(insn.field)
        repr_op = insn.operators[0]
        # FIXME: There's no real reason why differentiation is special,
        # execution-wise.
        # This should be unified with map_elementwise_linear, which should
        # be extended to support batching.

        discr = self.discrwb.discr_from_dd(repr_op.dd_in)

        # FIXME: Enable
        # assert repr_op.dd_in == repr_op.dd_out
        assert repr_op.dd_in.domain_tag == repr_op.dd_out.domain_tag

        @memoize_in(self.discrwb, "reference_derivative_knl")
        def knl():
            knl = lp.make_kernel(
                """{[imatrix,k,i,j]:
                    0<=imatrix<nmatrices and
                    0<=k<nelements and
                    0<=i,j<nunit_nodes}""",
                """
                result[imatrix, k, i] = sum(
                        j, diff_mat[imatrix, i, j] * vec[k, j])
                """,
                default_offset=lp.auto, name="diff")

            knl = lp.split_iname(knl, "i", 16, inner_tag="l.0")
            return lp.tag_inames(knl, dict(k="g.0"))

        noperators = len(insn.operators)
        result = discr.empty(
                queue=self.queue,
                dtype=field.dtype, extra_dims=(noperators,),
                allocator=self.bound_op.allocator)

        for grp in discr.groups:
            if grp.nelements == 0:
                continue

            matrices = repr_op.matrices(grp)

            # FIXME: Should transfer matrices to device and cache them
            matrices_ary = np.empty((
                noperators, grp.nunit_nodes, grp.nunit_nodes))
            for i, op in enumerate(insn.operators):
                matrices_ary[i] = matrices[op.rst_axis]

            knl()(self.queue,
                    diff_mat=matrices_ary,
                    result=grp.view(result), vec=grp.view(field))

        return [(name, result[i]) for i, name in enumerate(insn.names)], []

    # }}}

# }}}


# {{{ bound operator

class BoundOperator(object):
    def __init__(self, discrwb, discr_code, eval_code, debug_flags, allocator=None):
        self.discrwb = discrwb
        self.discr_code = discr_code
        self.eval_code = eval_code
        self.operator_data_cache = {}
        self.debug_flags = debug_flags
        self.allocator = allocator

    def __str__(self):
        sep = 75 * "=" + "\n"
        return (
                sep
                + "DISCRETIZATION-SCOPE CODE\n"
                + sep
                + str(self.discr_code) + "\n"
                + sep
                + "PER-EVALUATION CODE\n"
                + sep
                + str(self.eval_code))

    def __call__(self, queue, **context):
        import pyopencl.array as cl_array

        def replace_queue(a):
            if isinstance(a, cl_array.Array):
                return a.with_queue(queue)
            else:
                return a

        from pytools.obj_array import with_object_array_or_scalar

        # {{{ discrwb-scope evaluation

        if any(
                (result_var.name not in
                    self.discrwb._discr_scoped_subexpr_name_to_value)
                for result_var in self.discr_code.result):
            # need to do discrwb-scope evaluation
            discrwb_eval_context = {}
            self.discr_code.execute(
                    ExecutionMapper(queue, discrwb_eval_context, self))

        # }}}

        new_context = {}
        for name, var in six.iteritems(context):
            new_context[name] = with_object_array_or_scalar(replace_queue, var)

        return self.eval_code.execute(
                ExecutionMapper(queue, new_context, self))

# }}}


# {{{ process_sym_operator function

def process_sym_operator(discrwb, sym_operator, post_bind_mapper=None,
        dumper=lambda name, sym_operator: None):

    import grudge.symbolic.mappers as mappers

    dumper("before-bind", sym_operator)
    sym_operator = mappers.OperatorBinder()(sym_operator)

    mappers.ErrorChecker(discrwb.mesh)(sym_operator)

    if post_bind_mapper is not None:
        dumper("before-postbind", sym_operator)
        sym_operator = post_bind_mapper(sym_operator)

    dumper("before-empty-flux-killer", sym_operator)
    sym_operator = mappers.EmptyFluxKiller(discrwb.mesh)(sym_operator)

    dumper("before-cfold", sym_operator)
    sym_operator = mappers.CommutativeConstantFoldingMapper()(sym_operator)

    # Work around https://github.com/numpy/numpy/issues/9438
    #
    # The idea is that we need 1j as an expression to survive
    # until code generation time. If it is evaluated and combined
    # with other constants, we will need to determine its size
    # (as np.complex64/128) within the expression. But because
    # of the above numpy bug, sized numbers are not likely to survive
    # expression building--so that's why we step in here to fix that.

    dumper("before-csize", sym_operator)
    sym_operator = mappers.ConstantToNumpyConversionMapper(
            real_type=discrwb.real_dtype.type,
            complex_type=discrwb.complex_dtype.type,
            )(sym_operator)

    # Ordering restriction:
    #
    # - Must run constant fold before first type inference pass, because zeros,
    # while allowed, violate typing constraints (because they can't be assigned
    # a unique type), and need to be killed before the type inferrer sees them.

    # FIXME: Reenable type inference

    # from grudge.symbolic.mappers.type_inference import TypeInferrer
    # dumper("before-specializer", sym_operator)
    # sym_operator = mappers.OperatorSpecializer(
    #         TypeInferrer()(sym_operator)
    #         )(sym_operator)

    # Ordering restriction:
    #
    # - Must run OperatorSpecializer before performing the GlobalToReferenceMapper,
    # because otherwise it won't differentiate the type of grids (node or quadrature
    # grids) that the operators will apply on.

    dumper("before-global-to-reference", sym_operator)
    sym_operator = mappers.GlobalToReferenceMapper(discrwb.ambient_dim)(sym_operator)

    dumper("before-distributed", sym_operator)

    volume_mesh = discrwb.discr_from_dd("vol").mesh
    from meshmode.distributed import get_connected_partitions
    connected_parts = get_connected_partitions(volume_mesh)
    sym_operator = mappers.DistributedMapper(connected_parts)(sym_operator)

    # Ordering restriction:
    #
    # - Must specialize quadrature operators before performing inverse mass
    # contraction, because there are no inverse-mass-contracted variants of the
    # quadrature operators.

    dumper("before-imass", sym_operator)
    sym_operator = mappers.InverseMassContractor()(sym_operator)

    dumper("before-cfold-2", sym_operator)
    sym_operator = mappers.CommutativeConstantFoldingMapper()(sym_operator)

    # FIXME: Reenable derivative joiner
    # dumper("before-derivative-join", sym_operator)
    # sym_operator = mappers.DerivativeJoiner()(sym_operator)

    dumper("process-finished", sym_operator)

    return sym_operator

# }}}


def bind(discr, sym_operator, post_bind_mapper=lambda x: x,
        debug_flags=set(), allocator=None):
    # from grudge.symbolic.mappers import QuadratureUpsamplerRemover
    # sym_operator = QuadratureUpsamplerRemover(self.quad_min_degrees)(
    #         sym_operator)

    stage = [0]

    def dump_optemplate(name, sym_operator):
        if "dump_optemplate_stages" in debug_flags:
            from grudge.tools import open_unique_debug_file
            from grudge.optemplate import pretty
            open_unique_debug_file("%02d-%s" % (stage[0], name), ".txt").write(
                    pretty(sym_operator))
            stage[0] += 1

    sym_operator = process_sym_operator(
            discr,
            sym_operator,
            post_bind_mapper=post_bind_mapper,
            dumper=dump_optemplate)

    from grudge.symbolic.compiler import OperatorCompiler
    discr_code, eval_code = OperatorCompiler(discr)(sym_operator)

    bound_op = BoundOperator(discr, discr_code, eval_code,
            debug_flags=debug_flags, allocator=allocator)

    if "dump_op_code" in debug_flags:
        from grudge.tools import open_unique_debug_file
        open_unique_debug_file("op-code", ".txt").write(
                str(bound_op))

    if "dump_dataflow_graph" in debug_flags:
        bound_op.code.dump_dataflow_graph()

    return bound_op

# vim: foldmethod=marker
