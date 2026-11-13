# The Cython code is split into several extension modules so that they
# can be compiled in parallel; the trace callbacks are all in
# _line_profiler
set(module_names "_line_profiler" "_profiler_base" "_sys_monitoring")

foreach(module_name IN LISTS module_names)
  set(cython_source
    "${CMAKE_CURRENT_SOURCE_DIR}/${module_name}.pyx"
    "${CMAKE_CURRENT_SOURCE_DIR}/python25.pxd"
  )

  # Translate Cython into C/C++
  add_cython_target(${module_name} "${cython_source}" C OUTPUT_VAR sources)

  # Add any other non-cython dependencies to the sources
  if(module_name STREQUAL "_line_profiler")
    list(APPEND sources
      "${CMAKE_CURRENT_SOURCE_DIR}/timers.c"
      "${CMAKE_CURRENT_SOURCE_DIR}/c_trace_callbacks.c"
    )
  elseif(module_name STREQUAL "_sys_monitoring")
    list(APPEND sources
      "${CMAKE_CURRENT_SOURCE_DIR}/c_trace_callbacks.c"
    )
  endif()
  message(STATUS "[OURS] ${module_name} sources = ${sources}")

  # Create C++ library. Specify include dirs and link libs as normal
  add_library(${module_name} MODULE ${sources})
  target_include_directories(${module_name} PUBLIC
    ${PYTHON_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}  # for the pure c files defined here
  )

  # Transform the C++ library into an importable python module
  python_extension_module(${module_name})

  # Install the C++ module to the correct relative location
  # (this will be an inplace build if you use `pip install -e`)
  file(RELATIVE_PATH _install_dest "${CMAKE_SOURCE_DIR}" "${CMAKE_CURRENT_SOURCE_DIR}")
  message(STATUS "[OURS] _install_dest = ${_install_dest}")
  install(TARGETS ${module_name} LIBRARY DESTINATION "${_install_dest}/")
endforeach()
