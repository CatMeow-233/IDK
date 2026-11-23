struct ring { unsigned fill; unsigned char data[16]; };
void ring_add(struct ring *r, unsigned char c)
{
    r->data[r->fill] = c;
    r->fill++;
    if (r->fill > 16) {
        r->fill = 0;
    }
}
unsigned char ring_peek(const struct ring *r) { return r->data[r->fill]; }
