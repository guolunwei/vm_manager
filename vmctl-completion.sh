#!/bin/bash

_get_vm_names() {
  vmctl.sh list --all 2> /dev/null
}

_complete_vm_names() {
  local cur="$1"

  COMPREPLY=()

  while IFS= read -r vm; do
    [[ "$vm" == "$cur"* ]] || continue

    COMPREPLY+=("${vm// /\\ }")

  done < <(_get_vm_names)
}

_vmctl_complete() {
  local COMP_WORDBREAKS=${COMP_WORDBREAKS// /}
  local cur prev words cword

  _get_comp_words_by_ref cur prev words cword

  # get subcommands
  local subcommands
  mapfile -t subcommands_array < <(
    vmctl.sh 2> /dev/null \
      | awk '/^Available commands:/{flag=1;next} /^For example:/{flag=0} flag {print $1}' \
      | sort | uniq
  )
  subcommands="${subcommands_array[*]}"

  case $cword in
    1)
      mapfile -t COMPREPLY < <(compgen -W "$subcommands" -- "$cur")
      ;;
    *)
      case ${words[1]} in
        clone | remove | start | stop | restart | ssh)
          _complete_vm_names "$cur"
          ;;
        setip)
          case $cword in
            2)
              _complete_vm_names "$cur"
              ;;
            3)
              COMPREPLY=()
              ;;
          esac
          ;;
        list)
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
