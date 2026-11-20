```json
{"files": {"src/safe_array.c": "```c\n#include \"safe_array.h\"\nint safe_array_get(const int *arr, size_t len, size_t idx, int *out)\n{\n    if (!arr || !out) return SAFE_ARRAY_ERR_NULL;\n    if (idx >= len) return SAFE_ARRAY_ERR_RANGE;\n    *out = arr[idx];\n    return SAFE_ARRAY_OK;\n}\n```"},
 "notes": "Uses early returns. Example usage:\n```c\nint v; safe_array_get(a, 3, 1, &v);\n```"}
```
