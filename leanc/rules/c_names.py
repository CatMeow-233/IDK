from __future__ import annotations

import re

__all__ = [
    "ALLOWED_IMPORTS",
    "COMPILER_RT_ARITHMETIC_HELPER_RE",
    "C_KEYWORDS",
    "DANGEROUS_NAMES",
    "HARNESS_INPUT_PREFIX",
    "LIBC_NAMES",
    "RESERVED_IDENTIFIER_RE",
    "TERMINATION_NAMES",
    "forbidden_definition",
    "is_dangerous_import",
    "is_verifier_hook",
]

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "memcpy", "memmove", "memset", "memcmp", "memchr",
        "strlen", "strnlen", "strcmp", "strncmp", "strcpy", "strncpy", "strcat", "strncat",
        "strchr", "strrchr", "strstr",
        "malloc", "calloc", "realloc", "free",
        "abs", "labs", "llabs",
        "__assert_fail",
        "printf", "fprintf", "puts", "putchar", "fputs", "fputc", "putc", "stdout", "stderr",
        "__errno_location", "__stack_chk_fail", "__stack_chk_guard",
    }
)

COMPILER_RT_ARITHMETIC_HELPER_RE = re.compile(
    r"^__(?:u?div|u?mod|mul|mulo|ashl|ashr|lshr|neg|u?cmp|popcount|parity|clz|ctz|ffs|bswap)[a-z]*[23]$"
)

TERMINATION_NAMES: frozenset[str] = frozenset(
    {
        "exit", "_exit", "_Exit", "quick_exit", "abort", "atexit", "at_quick_exit", "on_exit",
        "__cxa_atexit", "__cxa_finalize", "setjmp", "_setjmp", "__sigsetjmp", "sigsetjmp", "longjmp",
        "_longjmp", "siglongjmp", "__longjmp_chk", "raise", "kill", "signal", "sigaction", "thrd_exit",
        "pthread_exit", "__assert", "__assert_perror_fail",
    }
)

DANGEROUS_NAMES: frozenset[str] = frozenset(
    {
        "fork", "vfork", "clone", "clone3", "daemon", "execl", "execle", "execlp", "execv", "execve",
        "execvp", "execvpe", "execveat", "fexecve", "posix_spawn", "posix_spawnp", "system", "popen",
        "dlopen", "dlmopen", "dlsym", "dlvsym", "syscall", "ptrace", "socket", "socketpair", "connect",
        "bind", "listen", "accept", "accept4", "sendto", "recvfrom", "prctl", "setrlimit", "mprotect",
    }
)
_THREAD_OR_SANITIZER_API_RE = re.compile(r"^(?:pthread_|thrd_|__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer)_)")
_VERIFIER_HOOK_PREFIXES = ("__CPROVER", "__VERIFIER", "nondet_")
HARNESS_INPUT_PREFIX = "nondet_"
_HARNESS_ENTRY_POINT = "main"

LIBC_NAMES: frozenset[str] = (
    ALLOWED_IMPORTS
    | TERMINATION_NAMES
    | DANGEROUS_NAMES
    | frozenset(
        {
            "assert", "static_assert", "main", "aligned_alloc", "strdup", "strndup", "sprintf",
            "snprintf", "vprintf", "vfprintf", "vsnprintf", "getchar", "fgets", "fopen", "fclose",
            "fread", "fwrite", "read", "write", "open", "close", "rand", "srand", "time", "clock",
            "sleep", "usleep", "atoi", "atol", "strtol", "strtoul", "strtoll", "strtoull", "qsort",
            "bsearch", "errno", "offsetof", "NULL", "EOF", "true", "false", "bool", "isalpha",
            "isdigit", "isspace", "toupper", "tolower", "div", "ldiv",
        }
    )
)

C_KEYWORDS: frozenset[str] = frozenset(
    {
        "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum",
        "extern", "float", "for", "goto", "if", "inline", "int", "long", "register", "restrict", "return",
        "short", "signed", "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while", "alignas", "alignof", "typeof", "typeof_unqual", "constexpr", "nullptr",
        "thread_local",
    }
)

RESERVED_IDENTIFIER_RE = re.compile(r"^(?:__\w*|_[A-Z]\w*)$")


def is_verifier_hook(name: str) -> bool:
    return name.startswith(_VERIFIER_HOOK_PREFIXES)


def is_dangerous_import(name: str) -> bool:
    return name in DANGEROUS_NAMES or bool(_THREAD_OR_SANITIZER_API_RE.match(name))


def forbidden_definition(name: str) -> str | None:
    if name == _HARNESS_ENTRY_POINT:
        return "'main' belongs to the trusted harness / test driver"
    if name.startswith(HARNESS_INPUT_PREFIX):
        return f"'{name}' is a harness input generator: giving it a body narrows the symbolic inputs"
    if RESERVED_IDENTIFIER_RE.match(name):
        return f"'{name}' is a reserved identifier (C library / compiler / verifier hook)"
    if name in LIBC_NAMES:
        return f"'{name}' is a C library / CBMC model function"
    return None
