# shellcheck shell=bash
# Style a label on its destination stream. Keep paths and details plain.
term_line() {
    local role=$1 label=$2 detail=${3:-} code
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != dumb ]; then
        case "$role" in
            heading) code='1;36' ;;
            step) code=1 ;;
            removed) code=32 ;;
            would_remove) code=36 ;;
            kept) code=2 ;;
            skipped) code=33 ;;
            failed) code='1;31' ;;
        esac
        printf '\033[%sm%s\033[0m%s\n' "$code" "$label" "$detail"
    else
        printf '%s%s\n' "$label" "$detail"
    fi
}
