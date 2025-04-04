#!/bin/zsh

if [[ ! -d "timing/" ]]; then
    mkdir timing
fi

export PYOPENCL_CTX=1

export LOOPY_NO_CACHE=1
export PYOPENCL_NO_CACHE=1
export POCL_KERNEL_CACHE=0
export CUDA_CACHE_DISABLE=1

export ARGS="--lazy --resolution 100 --tpe --tfinal 1"

transform_status="FULL"

orders=(1 2 3 4 5 6 7)  # skip order = 3 to avoid bad reshape logic
test_case="vortex"

echo "========================================================="
    echo "TESTING $test_case.py"
    for i in "${orders[@]}"
    do
        echo "Testing order $i with TP transformations ON"
        eval "python $test_case.py --use-tp-transforms --order $i $ARGS" 2>&1 | \
            grep -a "walltime" > timing/$test_case-timing-$i-ON.txt
        cat timing/$test_case-timing-$i-ON.txt
        echo "done testing order $i"

        echo "Testing order $i with TP transformations OFF"
        eval "python $test_case.py --order $i $ARGS" 2>&1 | \
            grep -a "walltime" > timing/$test_case-timing-$i-$transform_status.txt
        cat timing/$test_case-timing-$i-$transform_status.txt
        echo "done testing order $i"
    done
echo "========================================================="

export ARGS="--lazy --resolution 15 --tpe --tfinal 10"

orders=(1 2 3 4 5 6 7)  # skip order = 3 to avoid bad reshape logic
test_case="acoustic_pulse"

echo "========================================================="
    echo "TESTING $test_case.py"
    for i in "${orders[@]}"
    do
        echo "Testing order $i with TP transformations ON"
        eval "python $test_case.py --use-tp-transforms --order $i $ARGS" 2>&1 | \
            grep -a "walltime" > timing/$test_case-timing-$i-ON.txt
        cat timing/$test_case-timing-$i-ON.txt
        echo "done testing order $i"

        echo "Testing order $i with TP transformations OFF"
        eval "python $test_case.py --order $i $ARGS" 2>&1 | \
            grep -a "walltime" > timing/$test_case-timing-$i-$transform_status.txt
        cat timing/$test_case-timing-$i-$transform_status.txt
        echo "done testing order $i"
    done
echo "========================================================="
