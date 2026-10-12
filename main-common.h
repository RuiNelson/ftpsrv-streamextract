/* Copyright (C) 2025 John Törnblom

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation; either version 3, or (at your option) any
later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; see the file COPYING. If not, see
<http://www.gnu.org/licenses/>.  */

#pragma once

#include <errno.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>

static inline void
ftp_print_usage(const char *progname) {
  printf("usage: %s [-p PORT] [-c CONNS] [-s STRIPE]\n", progname);
  puts("");
  puts("options:");
  puts("    -p PORT    Bind the socket server to the given PORT (default: 2121)");
  puts("    -c CONNS   Largest FTPMC connection count, 1-256 (default: 4)");
  puts("    -s STRIPE  Largest FTPMC stripe size in octets, at least 4096");
  puts("               (default: 65536)");
}

static inline int
ftp_parse_uint_arg(const char *arg, unsigned long max, unsigned long *out) {
  char *endptr = NULL;
  unsigned long v;

  if(!arg || !out) {
    return -1;
  }

  errno = 0;
  v = strtoul(arg, &endptr, 10);
  if(errno || endptr == arg || *endptr || v > max) {
    return -1;
  }

  *out = v;
  return 0;
}

static inline int
ftp_parse_port_arg(const char *arg, uint16_t *port_out) {
  char *endptr = NULL;
  unsigned long port;

  if(!arg || !port_out) {
    return -1;
  }

  errno = 0;
  port = strtoul(arg, &endptr, 10);
  if(errno || endptr == arg || *endptr || port == 0 || port > 65535UL) {
    return -1;
  }

  *port_out = (uint16_t)port;
  return 0;
}
