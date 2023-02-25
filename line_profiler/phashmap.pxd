from libcpp.utility cimport pair

cdef extern from "parallel_hashmap/parallel_hashmap/phmap.h" namespace "phmap" nogil:
    cdef cppclass flat_hash_map[T, U]:
        ctypedef T key_type
        ctypedef U mapped_type
        
        cppclass iterator
        cppclass iterator:
            iterator() except +
            iterator(iterator&) except +
            pair[T, U]& operator*()
            iterator operator++()
            iterator operator--()
            iterator operator++(int)
            iterator operator--(int)
            bint operator==(iterator)
            bint operator==(const_iterator)
            bint operator!=(iterator)
            bint operator!=(const_iterator)
        cppclass const_iterator:
            const_iterator() except +
            const_iterator(iterator&) except +
            operator=(iterator&) except +
            # correct would be const value_type& but this does not work
            # well with cython's code gen
            const pair[T, U]& operator*()
            const_iterator operator++()
            const_iterator operator--()
            const_iterator operator++(int)
            const_iterator operator--(int)
            bint operator==(iterator)
            bint operator==(const_iterator)
            bint operator!=(iterator)
            bint operator!=(const_iterator)

        bint operator==(flat_hash_map&, flat_hash_map&)
        bint operator!=(flat_hash_map&, flat_hash_map&)
        bint operator<(flat_hash_map&, flat_hash_map&)
        bint operator>(flat_hash_map&, flat_hash_map&)
        bint operator<=(flat_hash_map&, flat_hash_map&)
        bint operator>=(flat_hash_map&, flat_hash_map&)

        flat_hash_map() except +
        flat_hash_map(flat_hash_map&) except +
        U& operator[](const T&)
        iterator begin()
        const_iterator const_begin "begin"()
        const_iterator cbegin()
        void clear()
        iterator end()
        const_iterator const_end "end"()
        const_iterator cend()
        iterator erase(iterator)
        iterator find(const T&)
        size_t count(const T&)
        void reserve(size_t)
        
        size_t size()
        void swap() 

    cdef cppclass parallel_flat_hash_map[T, U]:
        ctypedef T key_type
        ctypedef U mapped_type
        
        cppclass iterator
        cppclass iterator:
            iterator() except +
            iterator(iterator&) except +
            pair[T, U]& operator*()
            iterator operator++()
            iterator operator--()
            iterator operator++(int)
            iterator operator--(int)
            bint operator==(iterator)
            bint operator==(const_iterator)
            bint operator!=(iterator)
            bint operator!=(const_iterator)
        cppclass const_iterator:
            const_iterator() except +
            const_iterator(iterator&) except +
            operator=(iterator&) except +
            # correct would be const value_type& but this does not work
            # well with cython's code gen
            const pair[T, U]& operator*()
            const_iterator operator++()
            const_iterator operator--()
            const_iterator operator++(int)
            const_iterator operator--(int)
            bint operator==(iterator)
            bint operator==(const_iterator)
            bint operator!=(iterator)
            bint operator!=(const_iterator)

        bint operator==(parallel_flat_hash_map&, parallel_flat_hash_map&)
        bint operator!=(parallel_flat_hash_map&, parallel_flat_hash_map&)
        bint operator<(parallel_flat_hash_map&, parallel_flat_hash_map&)
        bint operator>(parallel_flat_hash_map&, parallel_flat_hash_map&)
        bint operator<=(parallel_flat_hash_map&, parallel_flat_hash_map&)
        bint operator>=(parallel_flat_hash_map&, parallel_flat_hash_map&)

        parallel_flat_hash_map() except +
        parallel_flat_hash_map(parallel_flat_hash_map&) except +
        U& operator[](const T&)
        iterator begin()
        const_iterator const_begin "begin"()
        const_iterator cbegin()
        void clear()
        iterator end()
        const_iterator const_end "end"()
        const_iterator cend()
        iterator erase(iterator)
        iterator find(const T&)
        size_t count(const T&)
        void reserve(size_t)
        
        size_t size()
        void swap() 

    cdef cppclass parallel_flat_hash_set[T]:
        ctypedef T value_type
        
        cppclass iterator
        cppclass iterator:
            iterator() except +
            iterator(iterator&) except +
            value_type& operator*()
            iterator operator++()
            iterator operator--()
            iterator operator++(int)
            iterator operator--(int)
            bint operator==(iterator)
            bint operator==(const_iterator)
            bint operator!=(iterator)
            bint operator!=(const_iterator)
        cppclass const_iterator:
            const_iterator() except +
            const_iterator(iterator&) except +
            operator=(iterator&) except +
            # correct would be const value_type& but this does not work
            # well with cython's code gen
            const value_type& operator*()
            const_iterator operator++()
            const_iterator operator--()
            const_iterator operator++(int)
            const_iterator operator--(int)
            bint operator==(iterator)
            bint operator==(const_iterator)
            bint operator!=(iterator)
            bint operator!=(const_iterator)

        bint operator==(parallel_flat_hash_set&, parallel_flat_hash_set&)
        bint operator!=(parallel_flat_hash_set&, parallel_flat_hash_set&)
        bint operator<(parallel_flat_hash_set&, parallel_flat_hash_set&)
        bint operator>(parallel_flat_hash_set&, parallel_flat_hash_set&)
        bint operator<=(parallel_flat_hash_set&, parallel_flat_hash_set&)
        bint operator>=(parallel_flat_hash_set&, parallel_flat_hash_set&)

        parallel_flat_hash_set() except +
        parallel_flat_hash_set(parallel_flat_hash_set&) except +
        iterator begin()
        const_iterator const_begin "begin"()
        const_iterator cbegin()
        void clear()
        iterator end()
        const_iterator const_end "end"()
        const_iterator cend()
        iterator erase(iterator)
        iterator find(const T&)
        size_t count(const T&)
        pair[iterator, bint] insert(const T&) except +
        void reserve(size_t)
        
        size_t size()
        void swap() 
