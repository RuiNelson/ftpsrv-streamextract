#undef NDEBUG
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "cmd.h"
#include "io.h"

/* Linker wrappers exercise real loopback sockets, check setup order, and
 * inject option failures without changing production code. */
#ifdef __APPLE__
#define REAL_SYMBOL(name) __asm("_" name)
#include "socket-wrap.h"
#else
#define REAL_SYMBOL(name)
#endif
int __real_socket(int, int, int) REAL_SYMBOL("socket");
int __real_accept(int, struct sockaddr *, socklen_t *) REAL_SYMBOL("accept");
int __real_connect(int, const struct sockaddr *, socklen_t) REAL_SYMBOL("connect");
int __real_listen(int, int) REAL_SYMBOL("listen");
int __real_setsockopt(int, int, int, const void *, socklen_t) REAL_SYMBOL("setsockopt");
int __real_getsockopt(int, int, int, void *, socklen_t *) REAL_SYMBOL("getsockopt");
int __real_close(int) REAL_SYMBOL("close");

static struct {
  int send_set, recv_set, send_read, recv_read, send_timeout, recv_timeout;
} settings[64];
static int fail_set, fail_get, zero_get, clobber_close_errno;
static int connect_calls, listen_calls;

static void
reset_fd(int fd) {
  assert(fd >= 0 && fd < (int)(sizeof(settings) / sizeof(settings[0])));
  memset(&settings[fd], 0, sizeof(settings[fd]));
}

int
__wrap_socket(int domain, int type, int protocol) {
  int fd = __real_socket(domain, type, protocol);
  if(fd >= 0) reset_fd(fd);
  return fd;
}

int
__wrap_accept(int fd, struct sockaddr *addr, socklen_t *len) {
  int accepted = __real_accept(fd, addr, len);
  if(accepted >= 0) reset_fd(accepted);
  return accepted;
}

int
__wrap_setsockopt(int fd, int level, int option, const void *value, socklen_t len) {
  if(level == SOL_SOCKET && option == fail_set) {
    errno = ENOBUFS;
    return -1;
  }
  int rc = __real_setsockopt(fd, level, option, value, len);
  if(rc == 0 && level == SOL_SOCKET) {
    if(option == SO_SNDBUF) settings[fd].send_set = 1;
    if(option == SO_RCVBUF) settings[fd].recv_set = 1;
    if(option == SO_SNDTIMEO) settings[fd].send_timeout = 1;
    if(option == SO_RCVTIMEO) settings[fd].recv_timeout = 1;
  }
  return rc;
}

int
__wrap_getsockopt(int fd, int level, int option, void *value, socklen_t *len) {
  if(level == SOL_SOCKET && option == fail_get) {
    errno = EIO;
    return -1;
  }
  int rc = __real_getsockopt(fd, level, option, value, len);
  if(rc == 0 && level == SOL_SOCKET) {
    if(option == SO_SNDBUF) settings[fd].send_read = 1;
    if(option == SO_RCVBUF) settings[fd].recv_read = 1;
    if(option == zero_get) *(int *)value = 0;
  }
  return rc;
}

static void
check_settings(int fd) {
  assert(settings[fd].send_set && settings[fd].recv_set);
  assert(settings[fd].send_read && settings[fd].recv_read);
  assert(settings[fd].send_timeout && settings[fd].recv_timeout);
}

int
__wrap_connect(int fd, const struct sockaddr *addr, socklen_t len) {
  check_settings(fd);
  connect_calls++;
  return __real_connect(fd, addr, len);
}

int
__wrap_listen(int fd, int backlog) {
  check_settings(fd);
  listen_calls++;
  return __real_listen(fd, backlog);
}

int
__wrap_close(int fd) {
  int rc = __real_close(fd);
  if(clobber_close_errno) errno = ERANGE;
  return rc;
}

static int
make_listener(struct sockaddr_in *addr) {
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  assert(fd >= 0);
  memset(addr, 0, sizeof(*addr));
  addr->sin_family = AF_INET;
  addr->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  assert(bind(fd, (struct sockaddr *)addr, sizeof(*addr)) == 0);
  assert(__real_listen(fd, 1) == 0);
  socklen_t len = sizeof(*addr);
  assert(getsockname(fd, (struct sockaddr *)addr, &len) == 0);
  return fd;
}

static void
check_reply(int fd, const char *prefix) {
  char reply[512];
  size_t n = 0;
  do {
    assert(n < sizeof(reply) - 1);
    assert(read(fd, &reply[n], 1) == 1);
  } while(reply[n++] != '\n');
  reply[n] = 0;
  assert(strncmp(reply, prefix, strlen(prefix)) == 0);
  assert(recv(fd, reply, sizeof(reply), MSG_DONTWAIT) == -1);
  assert(errno == EAGAIN || errno == EWOULDBLOCK);
}

int
main(void) {
  const int buffer_options[] = {SO_SNDBUF, SO_RCVBUF};
  const int timeout_options[] = {SO_SNDTIMEO, SO_RCVTIMEO};
  struct sockaddr_in addr;
  ftp_env_t env = {.data_fd = -1, .passive_fd = -1};
  int listener = make_listener(&addr);
  int peer = socket(AF_INET, SOCK_STREAM, 0);
  assert(peer >= 0);
  assert(__real_connect(peer, (struct sockaddr *)&addr, sizeof(addr)) == 0);
  env.active_fd = accept(listener, NULL, NULL);
  assert(env.active_fd >= 0);
  assert(pthread_mutex_init(&env.ctrl_mutex, NULL) == 0);
  close(listener);

  for(size_t i = 0; i < 2; i++) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int before, after;
    socklen_t len = sizeof(before);
    assert(__real_getsockopt(fd, SOL_SOCKET, buffer_options[i], &before, &len) == 0);
    fail_set = buffer_options[i];
    assert(io_set_socket_opts(fd, 1) == 0);
    len = sizeof(after);
    assert(__real_getsockopt(fd, SOL_SOCKET, buffer_options[i], &after, &len) == 0);
    assert(before == after && after > 0);
    fail_set = 0;
    fail_get = buffer_options[i];
    assert(io_set_socket_opts(fd, 1) == -1 && errno == EIO);
    fail_get = 0;
    zero_get = buffer_options[i];
    assert(io_set_socket_opts(fd, 1) == -1 && errno == EIO);
    zero_get = 0;
    fail_set = timeout_options[i];
    assert(io_set_socket_opts(fd, 1) == -1 && errno == ENOBUFS);
    fail_set = 0;
    close(fd);
  }

  listener = make_listener(&env.data_addr);
  // Cover the socket created by PORT and recreation on subsequent transfers.
  env.data_fd = socket(AF_INET, SOCK_STREAM, 0);
  for(int i = 0; i < 2; i++) {
    assert(ftp_data_open(&env) == 0);
    int accepted = accept(listener, NULL, NULL);
    assert(accepted >= 0);
    assert(ftp_data_close(&env) == 0);
    close(accepted);
  }
  assert(connect_calls == 2);
  fail_set = SO_SNDTIMEO;
  clobber_close_errno = 1;
  assert(ftp_data_open(&env) == -1 && errno == ENOBUFS);
  assert(env.data_fd == -1 && connect_calls == 2);
  clobber_close_errno = fail_set = 0;
  close(listener);

  for(int extended = 0; extended < 2; extended++) {
    ftp_command_fn_t *passive = extended ? ftp_cmd_EPSV : ftp_cmd_PASV;
    fail_get = SO_RCVBUF;
    assert(passive(&env, "") == 0);
    assert(env.passive_fd == -1);
    check_reply(peer, "550 ");
    assert(ftp_cmd_NOOP(&env, "") == 0);
    check_reply(peer, "200 ");
    fail_get = 0;

    assert(passive(&env, "") == 0);
    check_reply(peer, extended ? "229 " : "227 ");
    socklen_t len = sizeof(addr);
    assert(getsockname(env.passive_fd, (struct sockaddr *)&addr, &len) == 0);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int data_peer = socket(AF_INET, SOCK_STREAM, 0);
    assert(data_peer >= 0);
    assert(__real_connect(data_peer, (struct sockaddr *)&addr, sizeof(addr)) == 0);
    assert(ftp_data_open(&env) == 0);
    check_settings(env.data_fd);
    assert(env.passive_fd == -1);
    assert(ftp_data_close(&env) == 0);
    close(data_peer);
  }
  assert(listen_calls == 2);

  close(env.active_fd);
  close(peer);
  pthread_mutex_destroy(&env.ctrl_mutex);
  puts("Socket ordering, buffer fallback, and failure handling checks passed.");
  return 0;
}
