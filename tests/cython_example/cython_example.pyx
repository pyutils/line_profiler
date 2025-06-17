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
