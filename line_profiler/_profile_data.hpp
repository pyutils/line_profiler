// Profiling data structures for line_profiler's trace callbacks.
//
// Instead of hash maps keyed by (code block, line number) hashes, the
// data of each profiler for each code block is an array of per-line
// timings, found by line number through a small index (see LineIndex), and
// code objects find their block through a pointer cached on the code
// object itself (see CodeCache), so recording a line event is only a few
// pointer dereferences and array accesses.

#ifndef LINE_PROFILER_PROFILE_DATA_HPP
#define LINE_PROFILER_PROFILE_DATA_HPP

#include <algorithm>
#include <cstdlib>
#include <utility>
#include <vector>

#include "Python_wrapper.h"

namespace line_profiler {

// Timings of one (profiled) line of one code block, for one profiler
struct LineData {
    long nhits;
    PY_LONG_LONG total_time;
    int lineno;
};

// Maps the line numbers of a code block to the slots of their LineData
// in ProfileEntry.lines.  A slot never changes once assigned.
// The representation is chosen when lines are registered, so that the
// trace callbacks only do a lookup:
// - Usually, the lines of a code block are dense, and a table indexed by
//   the line number (relative to the first line) is used;
// - If the line numbers are spread out disproportionately (i.e. the
//   table would mostly be empty), a vector of (line number, slot) pairs
//   sorted by line number is binary-searched instead, so that the memory
//   used is proportional to the number of lines, not their span.
class LineIndex {
public:
    // Returns: the slot of the line, -1 if it isn't registered
    inline int find(int lineno) const
    {
        if (dense) {
            unsigned offset = (unsigned)(lineno - first_line);
            return (offset < (unsigned)table.size()) ? table[offset] : -1;
        }
        return find_sparse(lineno);
    }

    // Note: the line mustn't already be registered
    void insert(int lineno, int slot)
    {
        auto pair = std::make_pair(lineno, slot);
        sorted.insert(
            std::lower_bound(sorted.begin(), sorted.end(), pair), pair);
        int first = sorted.front().first, last = sorted.back().first;
        size_t span = (size_t)last - (size_t)first + 1;
        if (span > MAX_SPAN_PER_LINE * sorted.size() + MIN_DENSE_SPAN) {
            // Too sparse for a table
            dense = false;
            std::vector<int>().swap(table);
        } else if (dense && first == first_line) {
            if (span > table.size()) table.resize(span, -1);
            table[lineno - first_line] = slot;
        } else {  // (Re)build the table
            dense = true;
            first_line = first;
            table.assign(span, -1);
            for (const auto &registered : sorted) {
                table[registered.first - first] = registered.second;
            }
        }
    }

    // Returns: the number of entries allocated (for checking the memory
    // use in tests)
    inline size_t num_entries() const { return table.size() + sorted.size(); }

    // Returns: whether it's a table (for tests)
    inline bool is_dense() const { return dense; }

    // The size of a table is at most this many entries per line (plus
    // MIN_DENSE_SPAN), i.e. 4 bytes per line it'd cost relative to the
    // sorted vector
    static const size_t MAX_SPAN_PER_LINE = 4;
    static const size_t MIN_DENSE_SPAN = 64;

private:
    // (Kept out of line, so that the table lookup inlined into the trace
    // callbacks stays small)
#if defined(__GNUC__) || defined(__clang__)
    __attribute__((noinline))
#elif defined(_MSC_VER)
    __declspec(noinline)
#endif
    int find_sparse(int lineno) const
    {
        auto it = std::lower_bound(
            sorted.begin(), sorted.end(), std::make_pair(lineno, -1));
        return (it != sorted.end() && it->first == lineno) ? it->second : -1;
    }

    bool dense = true;
    // Line number of table[0]
    int first_line = 0;
    std::vector<int> table;
    // (line number, slot), sorted by line number
    std::vector<std::pair<int, int>> sorted;
};

// A thread's record of the line last executed in a frame of a code block
// (whose time is to be recorded at the next event in the frame)
struct LastLine {
    unsigned long thread_ident;
    // Identifies the frame (see inner_trace_callback() in
    // _line_profiler.pyx), so that the records of recursive calls don't
    // overwrite each other's
    const void *frame_key;
    // Slot of the line in ProfileEntry.lines, -1 if there's no record
    // (in which case the record is free for the thread's other frames)
    int line_index;
    PY_LONG_LONG time;
};

// A profiler's data for a code block
struct ProfileEntry {
    PyObject *profiler;  // Borrowed
    Py_hash_t block_hash;
    // The data of the (non-negative) lines, in the order they were
    // registered; see index
    std::vector<LineData> lines;
    // Maps line numbers to their slots in lines
    LineIndex index;
    // The negative line numbers registered, i.e. the -1 that
    // PyCode_Addr2Line() gives for the bytecode without a line (e.g. the
    // padding added by add_function(), and exception-handling and
    // generator bytecode).  No events are ever recorded for them, and
    // they are kept out of index, where they would make almost every
    // padded function's line numbers look too sparse for a table.
    std::vector<int> lineless;
    // Usually just the one frame of one thread, so a linear search is the
    // fastest
    std::vector<LastLine> last_lines;

    ProfileEntry(PyObject *profiler, Py_hash_t block_hash)
        : profiler(profiler), block_hash(block_hash) {}

    // Returns: whether the line wasn't already registered
    bool register_line(int lineno)
    {
        if (lineno < 0) {
            for (int registered : lineless) {
                if (registered == lineno) return false;
            }
            lineless.push_back(lineno);
            return true;
        }
        if (index.find(lineno) >= 0) return false;
        index.insert(lineno, (int)lines.size());
        lines.push_back(LineData{0, 0, lineno});
        return true;
    }

    // Returns: the slot of lineno in lines if it is registered, else -1
    inline int line_index(int lineno) const
    {
        return index.find(lineno);
    }

    // Returns: the record of the thread for the frame, NULL if there
    // isn't any
    // Note: the records are searched from the back, where those of the
    // innermost frames of recursive calls are
    inline LastLine *find_last_line(unsigned long thread_ident,
                                    const void *frame_key)
    {
        for (auto it = last_lines.rbegin(); it != last_lines.rend(); ++it) {
            if (it->line_index >= 0 && it->thread_ident == thread_ident
                    && it->frame_key == frame_key) {
                return &*it;
            }
        }
        return NULL;
    }

    // Returns: the record of the thread for the frame if there is one,
    // else a free slot for it (with line_index -1)
    inline LastLine *get_last_line(unsigned long thread_ident,
                                   const void *frame_key)
    {
        LastLine *free_slot = NULL;
        for (auto it = last_lines.rbegin(); it != last_lines.rend(); ++it) {
            if (it->thread_ident != thread_ident) continue;
            if (it->line_index < 0) {
                if (free_slot == NULL) free_slot = &*it;
            } else if (it->frame_key == frame_key) {
                return &*it;
            }
        }
        if (free_slot == NULL) {
            last_lines.push_back(LastLine{thread_ident, frame_key, -1, 0});
            return &last_lines.back();
        }
        free_slot->frame_key = frame_key;
        return free_slot;
    }

    // Returns: whether the thread has any slots (i.e. it has run the
    // block while it was profiled)
    inline bool has_thread(unsigned long thread_ident) const
    {
        for (const LastLine &last : last_lines) {
            if (last.thread_ident == thread_ident) return true;
        }
        return false;
    }

    // Returns: the thread's record of the most recently executed line,
    // NULL if there isn't any
    inline const LastLine *latest_last_line(unsigned long thread_ident) const
    {
        const LastLine *latest = NULL;
        for (const LastLine &last : last_lines) {
            if (last.line_index >= 0 && last.thread_ident == thread_ident
                    && (latest == NULL || last.time > latest->time)) {
                latest = &last;
            }
        }
        return latest;
    }

    // Drop all the records of the thread
    inline void clear_last_lines(unsigned long thread_ident)
    {
        for (LastLine &last : last_lines) {
            if (last.thread_ident == thread_ident) last.line_index = -1;
        }
    }
};

// The data of all profilers for a code block, i.e. the code objects with
// the same block hash
struct Block {
    Py_hash_t block_hash;
    // The profilers' data, usually just the one
    std::vector<ProfileEntry *> entries;

    explicit Block(Py_hash_t block_hash) : block_hash(block_hash) {}
};

// Cached on each code object seen by the trace callbacks (see PEP 523
// "co_extra"), so that its block is found without hashing its bytecode
struct CodeCache {
    // Value of the registry generation when block was looked up; the
    // generation changes when blocks are added (since a code object
    // without a block may then have one) or removed (since the cached
    // block may then be freed)
    unsigned long generation;
    Py_hash_t block_hash;
    Block *block;  // NULL if the code isn't being profiled
};

// freefunc for the CodeCache objects
inline void free_code_cache(void *cache)
{
    std::free(cache);
}

inline CodeCache *new_code_cache(Py_hash_t block_hash)
{
    CodeCache *cache = (CodeCache *)std::malloc(sizeof(CodeCache));
    if (cache != NULL) {
        cache->generation = 0;
        cache->block_hash = block_hash;
        cache->block = NULL;
    }
    return cache;
}

}  // namespace line_profiler

#endif  // LINE_PROFILER_PROFILE_DATA_HPP
