#!/bin/bash

usage() {
    echo 
    echo "Usage:"
    echo "./run_test.sh /path/to/test/directory"
    echo "  Tests the script by converting every .level/.episode in a directory & subdirectories to JSON"
    echo -e "  Success when:\n  - Script doesn't crash\n  - Converting a file to & from JSON results in the same file"
}

run_test() {
    FILE=$1
    EXTENSION=$2
    EXTRA_ARGS=$3
    echo -n "  $FILE: "
    SUCCESS=1
    CRASH=0
    cp "${FILE}" "/tmp/test.${EXTENSION}"
    python3 rocket-tweaker.py /tmp/test.${EXTENSION} -o /tmp/test.json $EXTRA_ARGS &> /dev/null
    if [ $? != 0 ]; then
        SUCCESS=0
        CRASH=1
    fi
    python3 rocket-tweaker.py /tmp/test.json -o /tmp/test2.${EXTENSION} $EXTRA_ARGS &> /dev/null
    if [ $? != 0 ]; then
        SUCCESS=0
        CRASH=1
    fi
    diff /tmp/test.${EXTENSION} /tmp/test2.${EXTENSION} &> /dev/null
    if [ $? != 0 ]; then
        SUCCESS=0
    fi

    if [ $SUCCESS = 1 ]; then
        echo -e "\033[0;32mSUCCESS\033[0m"
    else
        echo -en "\033[0;31mERROR\033[0m"
        if [ $CRASH = 1 ]; then
            echo -e " CRASH"
        else
            echo -e " File mismatch"
        fi
    fi

    rm /tmp/test.${EXTENSION}
    rm /tmp/test2.${EXTENSION}
    rm /tmp/test.json
}

if [ $# != 1 ]; then
    echo "Incorrect args"
    usage
    exit 1
fi
if [ ! -d "${1}" ]; then
    echo "\"${1}\" is not a directory"
    usage
    exit 1
fi

DIR="${1}"

echo "Running tests on \`.episode\` files:"
for FILE in "${DIR}"/*.episode; do
    [ -e "${FILE}" ] || continue
    run_test "${FILE}" "episode"
done
# With no wild card in there's no file name before the `.`
for FILE in "${DIR}"/.episode; do
    [ -e "${FILE}" ] || continue
    run_test "${FILE}" "episode"
done
# Loop over subdirectories in case the workshop folder is chosen
for FILE in "${DIR}"/*/*.episode; do
    [ -e "${FILE}" ] || continue
    run_test "${FILE}" "episode"
done
# With no wild card in there's no file name before the `.``
for FILE in "${DIR}"/*/.episode; do
    [ -e "${FILE}" ] || continue
    run_test "${FILE}" "episode"
done
echo
echo "Running tests on \`.level\` files:"
for FILE in "${DIR}"/*.level; do
    [ -e "${FILE}" ] || continue
    run_test "${FILE}" "level"
done
# Loop over subdirectories in case the workshop folder is chosen
for FILE in "${DIR}"/*/*.level; do
    [ -e "${FILE}" ] || continue
    run_test "${FILE}" "level"
done