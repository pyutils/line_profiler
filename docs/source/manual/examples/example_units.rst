Timing Units
------------

This example demonstrates how you can change the units in which the time is
reported.

Write the following demo script to disk

.. code:: bash

   echo "if 1:
        from line_profiler import profile

        @profile
        def is_prime(n):
            max_val = n ** 0.5
            stop = int(max_val + 1)
            for i in range(2, stop):
                if n % i == 0:
                    return False
            return True


        def find_primes(size):
            primes = []
            for n in range(size):
                flag = is_prime(n)
                if flag:
                    primes.append(n)
            return primes


        def main():
            print('start calculating')
            primes = find_primes(10)
            primes = find_primes(1000)
            primes = find_primes(100000)
            print(f'done calculating. Found {len(primes)} primes.')


        if __name__ == '__main__':
            main()
   " > script.py


Run the script with line profiling on. To change the unit in which time is
reported use the ``--unit`` command line argument. The following example shows
4 variants:

.. code:: bash
   LINE_PROFILE=1 python script.py

   # Use different values for the unit report
   python -m line_profiler -rtmz --unit 1 profile_output.lprof
   python -m line_profiler -rtmz --unit 1e-3 profile_output.lprof
   python -m line_profiler -rtmz --unit 1e-6 profile_output.lprof
   python -m line_profiler -rtmz --unit 1e-9 profile_output.lprof


You will notice the relevant difference in the output lines:


.. code::


    ==============
    unit 1 variant
    ==============

    Timer unit: 1 s

         ...

         6    101010          0.0      0.0      3.6              max_val = n ** 0.5
         7    101010          0.1      0.0      4.0              stop = int(max_val + 1)

         ...

    =================
    unit 1e-3 variant
    =================

    Timer unit: 0.001 s

         ...

         6    101010         46.6      0.0      3.6              max_val = n ** 0.5
         7    101010         51.5      0.0      4.0              stop = int(max_val + 1)

         ...

    =================
    unit 1e-6 variant
    =================

    Timer unit: 1e-06 s

         ...

         6    101010      46558.2      0.5      3.6              max_val = n ** 0.5
         7    101010      51491.7      0.5      4.0              stop = int(max_val + 1)

         ...

    =================
    unit 1e-9 variant
    =================

    Timer unit: 1e-09 s

         ...

         6    101010   46558246.0    460.9      3.6              max_val = n ** 0.5
         7    101010   51491716.0    509.8      4.0              stop = int(max_val + 1)

         ...
