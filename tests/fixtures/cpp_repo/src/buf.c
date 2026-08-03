// Fixed-size buffer copy helpers (fixture for secagent UC3 static analysis).
#include <string.h>

static char g_buffer[16];

void copy_into(const char *src) {
    // Unbounded copy into a fixed-size buffer — classic overflow.
    strcpy(g_buffer, src);
}

int scale(int numerator, int denominator) {
    // Division without a zero check.
    return numerator / denominator;
}

char at(int index) {
    // Index not validated against bounds.
    return g_buffer[index];
}
