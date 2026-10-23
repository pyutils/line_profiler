// Profiling data structures for line_profiler's trace callbacks.
//
// Instead of hash maps keyed by (code block, line number) hashes, the
// data of each profiler for each code block is an array indexed by line
// number, and code objects find their block through a pointer cached on
// the code object itself (see CodeCache), so recording a line event is
// only a few pointer dereferences and array accesses.

#ifndef LINE_PROFILER_PROFILE_DATA_HPP
#define LINE_PROFILER_PROFILE_DATA_HPP

#include <cstdlib>
#include <vector>

#include "Python_wrapper.h"

namespace line_profiler {

// Timings of one line of one code block, for one profiler
struct LineData {
    long nhits;
    PY_LONG_LONG total_time;
    // Whether the line is one of the code's lines (only those are
    // profiled)
    bool registered;
};

// A thread's record of the line last executed in a frame of a code block
// (whose time is to be recorded at the next event in the frame)
struct LastLine {
    unsigned long thread_ident;
    // Identifies the frame (see inner_trace_callback() in
    // _line_profiler.pyx), so that the records of recursive calls don't
    // overwrite each other's
    const void *frame_key;
    // Index into ProfileEntry.lines, -1 if there's no record (in which
    // case the slot is free for the thread's other frames)
    int line_index;
    PY_LONG_LONG time;
};

// A profiler's data for a code block
struct ProfileEntry {
    PyObject *profiler;  // Borrowed
    Py_hash_t block_hash;
    // Line number of lines[0]
    int first_line;
    std::vector<LineData> lines;
    // Usually just the one frame of one thread, so a linear search is the
    // fastest
    std::vector<LastLine> last_lines;

    ProfileEntry(PyObject *profiler, Py_hash_t block_hash)
        : profiler(profiler), block_hash(block_hash), first_line(0) {}

    // Returns: whether the line wasn't already registered
    bool register_line(int lineno)
    {
        if (lines.empty()) {
            first_line = lineno;
            lines.resize(1, LineData{0, 0, false});
        } else if (lineno < first_line) {
            lines.insert(lines.begin(), first_line - lineno,
                         LineData{0, 0, false});
            // The record indices shift with the lines
            for (LastLine &last : last_lines) {
                if (last.line_index >= 0) {
                    last.line_index += first_line - lineno;
                }
            }
            first_line = lineno;
        } else if (lineno - first_line >= (int)lines.size()) {
            lines.resize(lineno - first_line + 1, LineData{0, 0, false});
        }
        LineData &line = lines[lineno - first_line];
        if (line.registered) return false;
        line.registered = true;
        return true;
    }

    // Returns: the index of lineno in lines if it is registered, else
    // -1
    inline int line_index(int lineno) const
    {
        int index = lineno - first_line;
        if (index < 0 || index >= (int)lines.size()
                || !lines[index].registered) {
            return -1;
        }
        return index;
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
    // generation changes when new blocks are added, since a code object
    // without a block may then have one
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
