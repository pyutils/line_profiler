How to change units
-------------------

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


        @profile
        def find_primes(size):
            primes = []
            for n in range(size):
                flag = is_prime(n)
                if flag:
                    primes.append(n)
            return primes


        @profile
        def main():
            print('start calculating')
            primes = find_primes(10)
            primes = find_primes(1000)
            primes = find_primes(100000)
            print(f'done calculating. Found {len(primes)} primes.')


        if __name__ == '__main__':
            main()
   " > script.py


   LINE_PROFILE=1 python script.py

   # Use different values for the unit report
   python -m line_profiler -rtmz --unit 1 profile_output.lprof
   python -m line_profiler -rtmz --unit 1e-6 profile_output.lprof
   python -m line_profiler -rtmz --unit 1e-9 profile_output.lprof

