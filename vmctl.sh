#!/bin/bash

echo_ok(){   
    echo -n "$1" 
    echo -en "\\033[55G"
    echo -e "\033[32m[SUCCESS]\\033[0m"
}

echo_err(){   
    echo -n "$1" 
    echo -en "\\033[55G"
    echo -e "\033[31m[FAILED]\\033[0m"
} 

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
CONFIG_FILE="$SCRIPT_DIR/config.ini"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Configuration file $CONFIG_FILE not found."
    exit 1
fi
source "$CONFIG_FILE"

if [ ! -f "$SRC_VMX" ]; then
    echo "Source vmx does not exist."
    exit 1
fi

list_all_vms() {
    find "$BASE_DIR" -name "*.vmx" | awk -F / '{print $(NF-1)}'
}

list_vm() {
    vmrun list | awk -F'.' 'NR>1 {print $(NF-1)}' | awk -F'\' '{print $NF}'
}

clone_vm() {
    local vm_name="$1"

    vmrun -T ws clone "$SRC_VMX" "$BASE_DIR\\$vm_name\\$vm_name.vmx" linked -snapshot=$SNAPSHOT -cloneName=$vm_name
    if [ $? -ne 0 ]; then
        echo_err "Domain '$vm_name' create"
        exit 1
    fi

    vmrun -T ws start "$BASE_DIR\\$vm_name\\$vm_name.vmx" gui
    sleep 3
    vmrun -T ws -gu root -gp "$ROOTPASS" runScriptInGuest "$BASE_DIR\\$vm_name\\$vm_name.vmx" "bin/bash" \
    "hostnamectl set-hostname $vm_name"
    if [ $? -eq 0 ]; then
        echo_ok "Domain '$vm_name' create"
    fi
}

remove_vm() {
    local vm_name="$1"

    vmrun -T ws stop "$BASE_DIR\\$vm_name\\$vm_name.vmx" &> /dev/null
    rm -rf "${BASE_DIR}\\${vm_name}"
    #vmrun -T ws deleteVM "$BASE_DIR/$vm_name/$vm_name.vmx"

    if [ $? -eq 0 ]; then
        echo_ok "Domain '$vm_name' remove"
    else
        echo_err "Domain '$vm_name' remove"
        exit 1
    fi
    sed -i "/^$vm_name /d" ~/.vmctl/hosts 2>/dev/null
}

start_vm() {
    local vm_name="$1"

    vmrun -T ws start "$BASE_DIR\\$vm_name\\$vm_name.vmx" gui

    if [ $? -eq 0 ]; then
        echo_ok "Domain '$vm_name' start"
    else
        echo_err "Domain '$vm_name' start"
        exit 1
    fi
}

stop_vm() {
    local vm_name="$1"

    vmrun -T ws stop "$BASE_DIR\\$vm_name\\$vm_name.vmx"

    if [ $? -eq 0 ]; then
        echo_ok "Domain '$vm_name' stop"
    else
        echo_err "Domain '$vm_name' stop"
        exit 1
    fi
}

set_host_ip() {
    local vm_name="$1"
    local ip_addr="$2"

    vmrun -T ws -gu root -gp "$ROOTPASS" runScriptInGuest "$BASE_DIR\\$vm_name\\$vm_name.vmx" "bin/bash" \
    "nmcli connection modify eth0 ipv4.method manual ipv4.addresses $ip_addr/24 autoconnect yes; nmcli connection up eth0"
    
    if [ $? -eq 0 ]; then
        echo_ok "Domain '$vm_name' set ip to '$ip_addr'"

        mkdir -p ~/.vmctl
        sed -i "/^$vm_name /d" ~/.vmctl/hosts 2>/dev/null
        echo "$vm_name $ip_addr" >> ~/.vmctl/hosts
    else
        echo_err "Domain '$vm_name' set ip"
        exit 1
    fi

    echo "Deploying SSH key..."

    local ip_addr=$(grep -w "$vm_name" ~/.vmctl/hosts | awk '{print $2}')
    local key=$(cat ~/.ssh/id_rsa.pub)

    vmrun -T ws -gu root -gp "$ROOTPASS" runScriptInGuest "$BASE_DIR\\$vm_name\\$vm_name.vmx" "bin/bash" \
    "mkdir -p /root/.ssh && echo $key > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && chmod 700 /root/.ssh"

    if ssh -o StrictHostKeyChecking=no root@$ip_addr exit; then
        echo_ok "SSH key deployed"
    else
        echo_err "SSH key deployment failed"
    fi
}

ssh_vm() {
    local vm_name="$1"
    local ip_addr=$(grep -w "$vm_name" ~/.vmctl/hosts | awk '{print $2}')

    if [ -z "$ip_addr" ]; then
        echo_err "IP address of '$vm_name' not found."
        exit 1
    else
        ssh -o StrictHostKeyChecking=no root@$ip_addr
    fi
}

usage() {
    cat <<EOF
Usage: $0 [command] [arguments]

Available commands:
  list       List running or all VMs     $0 list [--all]
  clone      Clone one or more VMs       $0 clone <vm_name> [...]
  remove     Remove one or more VMs      $0 remove <vm_name> [...]
  start      Start one or more VMs       $0 start <vm_name> [...]
  stop       Stop one or more VMs        $0 stop <vm_name> [...]
  restart    Restart one or more VMs     $0 restart <vm_name> [...]
  setip      Set IP address of a VM      $0 setip <vm_name> <ip_addr>
  ssh        SSH into a VM               $0 ssh <vm_name>

For example:
  $0 clone web1 web2
  $0 setip web1 192.168.88.100
EOF
    exit 1
}

case $1 in
    "list")
    shift
    if [ -z "$1" ]; then
        list_vm
        exit 0
    elif [ "$1" == '--all' ]; then
        list_all_vms
        exit 0
    else
        usage
    fi
	;;
    "clone")
        if [ $# -lt 2 ]; then
            usage
        fi
        shift
        for vm_name in "$@"; do
            clone_vm "$vm_name"
        done
        ;;
    "remove")
        if [ $# -lt 2 ]; then
            usage
        fi
        shift
        for vm_name in "$@"; do
            remove_vm "$vm_name"
        done
        ;;
    "start")
        if [ $# -lt 2 ]; then
            usage
        fi
        shift
        for vm_name in "$@"; do
            start_vm "$vm_name"
        done
        ;;
    "stop")
        if [ $# -lt 2 ]; then
            usage
        fi
        shift
        for vm_name in "$@"; do
            stop_vm "$vm_name"
        done
        ;;
    "restart")
        if [ $# -lt 2 ]; then
            usage
        fi
        shift
        for vm_name in "$@"; do
            stop_vm "$vm_name"
            sleep 3
            start_vm "$vm_name"
        done
        ;;
    "setip")
        if [ $# -ne 3 ]; then
            usage
        fi
        shift
        name=$(echo "$*" | awk '{print $1}')
        ip=$(echo "$*" | awk '{print $2}')
        set_host_ip "$name" "$ip"
        ;;
    "ssh")
        if [ $# -ne 2 ]; then
            usage
        fi
        shift
        ssh_vm "$1"
        ;;
    *)
        usage
        ;;
esac
