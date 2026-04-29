#!/bin/bash

# Script for comparing 2 binary files 

if [ $# != 2 ]; then
    echo "Missing args"
    echo "./diff.sh /path/to/file1 /path/to/file2"
    exit 1
fi

xxd $1 > /tmp/1.hex
xxd $2 > /tmp/2.hex
diff -yW 140 /tmp/1.hex /tmp/2.hex
