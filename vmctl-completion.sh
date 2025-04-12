_vmctl_complete() {
    local cur prev words cword
    
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    words=("${COMP_WORDS[@]}")
    cword=$COMP_CWORD

    # 获取所有子命令
    local subcommands=$(vmctl.sh 2>/dev/null | awk '/^Available commands:/{flag=1;next} /^For example:/{flag=0} flag {print $1}' | sort | uniq)

    case $cword in
        1)
            # 补全主命令
            COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )
            ;;
        *)
            case ${words[1]} in
                clone|remove|start|stop|setip|ssh)
                    # 获取所有虚拟机名称
                    local vms=$(vmctl.sh list --all 2>/dev/null)
                    COMPREPLY=( $(compgen -W "$vms" -- "$cur") )
                    ;;
                setip)
                    if [ $cword -eq 2 ]; then
                        # 第二个参数补全虚拟机名
                        local vms=$(vmctl.sh list --all 2>/dev/null)
                        COMPREPLY=( $(compgen -W "$vms" -- "$cur") )
                    else
                        # 第三个参数不补全
                        COMPREPLY=()
                    fi
                    ;;
                *)
                    COMPREPLY=()
                    ;;
            esac
            ;;
    esac
}

complete -F _vmctl_complete vm
complete -F _vmctl_complete vmctl.sh
EOF
