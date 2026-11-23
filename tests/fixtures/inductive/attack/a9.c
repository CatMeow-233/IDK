struct node { struct node *next; int v; };
static struct node a, b;
int walk(struct node *p)
{
    int steps = 0;
    while (p != 0)
        __CPROVER_assigns(p, steps)
        __CPROVER_loop_invariant(p == &a || p == &b)
        __CPROVER_decreases((char *)p - (char *)0)
    {
        p = p->next;
        steps = 1;
    }
    return steps;
}
void entry(void) { a.next = &b; b.next = &a; walk(&a); }
