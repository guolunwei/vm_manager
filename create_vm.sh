#!/bin/bash
# Usage: ./create_vm.sh vm.txt
while read -r name ip
do
    ./vmctl.sh stop "$name" &> /dev/null
    ./vmctl.sh remove "$name"
    ./vmctl.sh clone "$name"
    ./vmctl.sh setip "$name" "$ip"
done < $1
