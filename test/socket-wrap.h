/* Test-only call redirection for linkers without GNU --wrap. */
#pragma once
#include <sys/socket.h>
#include <unistd.h>

#define socket __wrap_socket
#define accept __wrap_accept
#define connect __wrap_connect
#define listen __wrap_listen
#define setsockopt __wrap_setsockopt
#define getsockopt __wrap_getsockopt
#define close __wrap_close

int __wrap_socket(int, int, int);
int __wrap_accept(int, struct sockaddr *, socklen_t *);
int __wrap_connect(int, const struct sockaddr *, socklen_t);
int __wrap_listen(int, int);
int __wrap_setsockopt(int, int, int, const void *, socklen_t);
int __wrap_getsockopt(int, int, int, void *, socklen_t *);
int __wrap_close(int);
