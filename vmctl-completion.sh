#!/bin/bash

_vmctl_complete() {
  local cur prev words cword
  _get_comp_words_by_ref -n : cur prev words cword

  # get subcommands
  local subcommands
  mapfile -t subcommands_array < <(vmctl.sh 2> /dev/null | awk '/^Available commands:/{flag=1;next} /^For example:/{flag=0} flag {print $1}' | sort | uniq)
  subcommands="${subcommands_array[*]}"

  case $cword in
    1)
      # complete subcommands
      mapfile -t COMPREPLY < <(compgen -W "$subcommands" -- "$cur")
      ;;
    *)
      case ${words[1]} in
        clone | remove | start | stop | restart | ssh)
          # get all vms and handle spaces properly
          local vms_array
          if mapfile -t vms_array < <(vmctl.sh list --all 2> /dev/null); then
            # Use IFS to join array with newlines for compgen
            local IFS=$'\n'
            mapfile -t COMPREPLY < <(compgen -W "${vms_array[*]}" -- "$cur")
          else
            COMPREPLY=()
          fi
          ;;
        setip)
          case $cword in
            2)
              # complete vm name
              local vms_array
              if mapfile -t vms_array < <(vmctl.sh list --all 2> /dev/null); then
                local IFS=$'\n'
                mapfile -t COMPREPLY < <(compgen -W "${vms_array[*]}" -- "$cur")
              else
                COMPREPLY=()
              fi
              ;;
            3)
              # not complete ip
              COMPREPLY=()
              ;;
            *)
              COMPREPLY=()
              ;;
          esac
          ;;
        list)
          # complete list options
          if [[ "$cur" == -* ]]; then
            mapfile -t COMPREPLY < <(compgen -W "--all" -- "$cur")
          else
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
