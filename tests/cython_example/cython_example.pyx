# cython: profile=True
# cython: linetrace=True
# distutils: define_macros=CYTHON_TRACE=1


ctypedef long double Float


def cos(Float x, int n):  # Start: cos
    cdef Float neg_xsq = -x * x
    cdef Float last_term = 1.
    cdef Float result = 0.
    for n in range(2, 2 * n, 2):
        result += last_term
        last_term *= neg_xsq / <Float>(n * (n - 1))
    return result + last_term   # End: cos


cpdef sin(Float x, int n):  # Start: sin
    cdef Float neg_xsq = -x * x
    cdef Float last_term = x
    cdef Float result = 0.
    for n in range(2, 2 * n, 2):
        result += last_term
        last_term *= neg_xsq / <Float>(n * (n + 1))
    return result + last_term  # End: sin


def add_one(int x):  # Start: add_one
    return x + 1  # End: add_one


def count_up(int n):  # Start: count_up
    cdef int total = 0
    for _ in range(n):
        total = add_one(total)
    return total  # End: count_up
