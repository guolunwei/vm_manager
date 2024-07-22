#!/bin/bash
while read -r name ip
do
    ./vm.sh stop "$name" &> /dev/null
    ./vm.sh remove "$name"
    ./vm.sh clone "$name"
    ./vm.sh setip "$name" "$ip"
done < ./vm.txt
