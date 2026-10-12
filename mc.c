/* Copyright (C) 2026 Rui Nelson

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

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include "io.h"
#include "log.h"
#include "mc.h"

/* Minimum ring size; it grows to 2 * N * S for large stripes. */
#ifndef FTP_MC_RING_SIZE
#define FTP_MC_RING_SIZE (1024 * 1024)
#endif

/* All buffers of one transfer: the ring plus the send and receive socket
   buffers of every connection. */
#ifndef FTP_MC_BUFFER_BUDGET
#define FTP_MC_BUFFER_BUDGET (16 * 1024 * 1024)
#endif

/* Time allowed to establish all N connections. */
#ifndef FTP_MC_CONNECT_TIMEOUT_SEC
#define FTP_MC_CONNECT_TIMEOUT_SEC 30
#endif

/* A transfer fails when no connection makes progress for this long. */
#ifndef FTP_MC_STALL_TIMEOUT_SEC
#define FTP_MC_STALL_TIMEOUT_SEC 60
#endif

/* Smallest socket buffer a connection may get within the budget. */
#ifndef FTP_MC_MIN_SOCK_BUFSIZE
#define FTP_MC_MIN_SOCK_BUFSIZE (128 * 1024)
#endif

#define MC_DEFAULT_STRIPE 4096
#define MC_IOV 64
#define MC_SEND_MAX (1024 * 1024)
#define MC_BLOCK_EOF 0x40
#define MC_BLOCK_MAX 65535

#ifdef MSG_NOSIGNAL
#define MC_SEND_FLAGS (MSG_NOSIGNAL | MSG_DONTWAIT)
#else
#define MC_SEND_FLAGS MSG_DONTWAIT
#endif

enum { MC_NONE, MC_PASSIVE, MC_ACTIVE };

/**
 * One data connection. q counts the octets of this connection's share of
 * the data stream (its substream) moved so far, without MODE B framing.
 **/
typedef struct mc_conn {
  int fd;
  int lfd;
  uint16_t port;
  int done;
  uint64_t q;
  unsigned char hdr[3];
  unsigned hdr_len;
  size_t blk_rem;
  int blk_eof;
} mc_conn_t;

struct ftp_mc {
  unsigned n;
  uint32_t stripe;
  int kind;
  int open;
  int aborted;
  struct in_addr addr;

  /* Transfer state; the ring is allocated only while xfer is set. */
  int xfer;
  int ctrl_watch;
  int ctrl_lost;
  unsigned char *ring;
  size_t ring_size;
  uint64_t head;     /* sending: octets of the data stream in the ring */
  uint64_t end;      /* data stream length, or the smallest end seen */
  int len_known;     /* sending: end is the final length */
  uint64_t flushed;  /* receiving: octets written to the file */
  struct timespec progress;
  struct pollfd *pfd;
  mc_conn_t conn[];
};

static unsigned mc_max_conn = FTP_MC_MAX_CONN;
static uint32_t mc_max_stripe = FTP_MC_MAX_STRIPE;


/**
 * Ring size for N connections with stripe S.
 **/
static uint64_t
mc_ring_bytes(unsigned n, uint64_t s) {
  uint64_t ring = 2 * (uint64_t)n * s;
  return ring < FTP_MC_RING_SIZE ? FTP_MC_RING_SIZE : ring;
}

/**
 * Largest stripe for N connections that keeps the ring and the smallest
 * socket buffers within the budget.
 **/
static uint64_t
mc_stripe_limit(unsigned n) {
  uint64_t sockets = 2 * (uint64_t)n * FTP_MC_MIN_SOCK_BUFSIZE;
  uint64_t limit = mc_max_stripe;

  if(sockets + FTP_MC_RING_SIZE > FTP_MC_BUFFER_BUDGET) {
    return 0;
  }
  if((FTP_MC_BUFFER_BUDGET - sockets) / (2 * (uint64_t)n) < limit) {
    limit = (FTP_MC_BUFFER_BUDGET - sockets) / (2 * (uint64_t)n);
  }
  return limit;
}

int
ftp_mc_configure(unsigned max_conn, uint32_t max_stripe) {
  unsigned saved_conn = mc_max_conn;
  uint32_t saved_stripe = mc_max_stripe;

  if(max_conn < 1 || max_conn > 256 || max_stripe < MC_DEFAULT_STRIPE ||
     FTP_MC_MIN_STRIPE > MC_DEFAULT_STRIPE) {
    return -1;
  }
  mc_max_conn = max_conn;
  mc_max_stripe = max_stripe;
  /* The default stripe must fit the budget with every connection. */
  if(mc_stripe_limit(max_conn) < MC_DEFAULT_STRIPE) {
    mc_max_conn = saved_conn;
    mc_max_stripe = saved_stripe;
    return -1;
  }
  mc_max_conn = max_conn;
  mc_max_stripe = max_stripe;
  return 0;
}

unsigned
ftp_mc_max_conn(void) {
  return mc_max_conn;
}

int
ftp_mc_has_set(const ftp_env_t *env) {
  return env->mc && env->mc->kind != MC_NONE;
}

int
ftp_mc_xfer_active(const ftp_env_t *env) {
  return env->mc && env->mc->xfer;
}

int
ftp_mc_take_abort(ftp_env_t *env) {
  if(!env->mc || !env->mc->aborted) {
    return 0;
  }
  env->mc->aborted = 0;
  return 1;
}


/**
 * Stream offset of octet q of connection k's substream.
 **/
static inline uint64_t
mc_off(const ftp_mc_t *mc, unsigned k, uint64_t q) {
  uint64_t s = mc->stripe;
  return ((q / s) * mc->n + k) * s + q % s;
}

/**
 * Number of octets of connection k's substream below stream offset lim.
 * With lim = L this is b(k) of draft Section 6.4.
 **/
static inline uint64_t
mc_qmax(const ftp_mc_t *mc, unsigned k, uint64_t lim) {
  uint64_t s = mc->stripe;
  uint64_t round = s * mc->n;
  uint64_t rem = lim % round;
  uint64_t start = (uint64_t)k * s;
  uint64_t q = (lim / round) * s;

  if(rem > start) {
    q += rem - start < s ? rem - start : s;
  }
  return q;
}

/**
 * Describe the ring slots of connection k from substream octet q, below
 * stream offset lim, for at most max octets. Returns the iovec count.
 **/
static int
mc_iov(const ftp_mc_t *mc, unsigned k, uint64_t q, uint64_t lim, size_t max,
       struct iovec *iov, int iovmax, size_t *total) {
  uint64_t s = mc->stripe;
  size_t w = mc->ring_size;
  size_t tot = 0;
  int cnt = 0;

  while(tot < max && cnt < iovmax) {
    uint64_t x = mc_off(mc, k, q);
    if(x >= lim) {
      break;
    }
    uint64_t run = s - q % s;
    if(run > lim - x) {
      run = lim - x;
    }
    if(run > max - tot) {
      run = max - tot;
    }
    size_t idx = (size_t)(x % w);
    size_t first = w - idx < run ? w - idx : (size_t)run;
    iov[cnt].iov_base = mc->ring + idx;
    iov[cnt].iov_len = first;
    cnt++;
    if(first < run) {
      if(cnt == iovmax) {
        run = first;
      } else {
        iov[cnt].iov_base = mc->ring;
        iov[cnt].iov_len = (size_t)run - first;
        cnt++;
      }
    }
    q += run;
    tot += (size_t)run;
  }
  *total = tot;
  return cnt;
}


static void
mc_touch(ftp_mc_t *mc) {
  clock_gettime(CLOCK_MONOTONIC, &mc->progress);
}

/**
 * Milliseconds left before sec seconds have passed since start.
 **/
static int
mc_ms_left(const struct timespec *start, int sec) {
  struct timespec now;
  int64_t ms;

  clock_gettime(CLOCK_MONOTONIC, &now);
  ms = (int64_t)(now.tv_sec - start->tv_sec) * 1000 +
       (now.tv_nsec - start->tv_nsec) / 1000000;
  ms = (int64_t)sec * 1000 - ms;
  if(ms <= 0) {
    return 0;
  }
  return ms > INT_MAX ? INT_MAX : (int)ms;
}

/**
 * Look at pending control input during a transfer, without consuming it.
 * The control connection closing, or ABOR, ends the transfer at once.
 **/
static int
mc_ctrl_event(ftp_env_t *env, ftp_mc_t *mc) {
  char buf[128];
  ssize_t n;

  do {
    n = recv(env->active_fd, buf, sizeof(buf), MSG_PEEK | MSG_DONTWAIT);
  } while(n < 0 && errno == EINTR);

  if(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
    return 0;
  }
  if(n <= 0) {
    mc->ctrl_lost = 1;
    errno = ECONNRESET;
    return -1;
  }
  for(ssize_t i = 0; i + 4 <= n; i++) {
    /* At the start of a line, possibly after Telnet IP and Synch. */
    if((!i || buf[i - 1] == '\n' || (unsigned char)buf[i - 1] >= 0xf0) &&
       !strncasecmp(buf + i, "ABOR", 4)) {
      mc->aborted = 1;
      errno = ECONNRESET;
      return -1;
    }
  }
  /* Another command is waiting; it runs after the transfer. */
  mc->ctrl_watch = 0;
  return 0;
}

/**
 * Wait for events on pfd[0..nfds) where pfd[0] is the control connection.
 * Fails with ETIMEDOUT once sec seconds have passed since start.
 **/
static int
mc_poll(ftp_env_t *env, ftp_mc_t *mc, nfds_t nfds,
        const struct timespec *start, int sec) {
  int ms = mc_ms_left(start, sec);

  if(!ms) {
    errno = ETIMEDOUT;
    return -1;
  }
  mc->pfd[0].fd = mc->ctrl_watch ? env->active_fd : -1;
  mc->pfd[0].events = POLLIN;
  mc->pfd[0].revents = 0;
  if(poll(mc->pfd, nfds, ms) < 0) {
    return errno == EINTR ? 0 : -1;
  }
  if(mc->pfd[0].fd >= 0 && mc->pfd[0].revents) {
    return mc_ctrl_event(env, mc);
  }
  return 0;
}


/**
 * Send and receive buffer size of each socket: what the budget leaves
 * after the ring, shared by the 2 * N buffers, up to the size used for a
 * single data connection.
 **/
static int
mc_sock_bufsize(const ftp_mc_t *mc) {
  uint64_t size = (FTP_MC_BUFFER_BUDGET - mc_ring_bytes(mc->n, mc->stripe)) /
                  (2 * (uint64_t)mc->n);
  return (int)(size > IO_SOCK_DATA_BUFSIZE ? IO_SOCK_DATA_BUFSIZE : size);
}

/**
 * Options for a connected data socket: non-blocking, no Nagle delay on
 * the final short segment, and keepalive so that a vanished client frees
 * an idle persistent set.
 **/
static int
mc_sock_opts(int fd) {
  int flags = fcntl(fd, F_GETFL, 0);

  if(flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
    return -1;
  }
#ifdef SO_NOSIGPIPE
  (void)setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &(int){1}, sizeof(int));
#endif
#ifdef TCP_NODELAY
  (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &(int){1}, sizeof(int));
#endif
#ifdef SO_KEEPALIVE
  (void)setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &(int){1}, sizeof(int));
#ifdef TCP_KEEPIDLE
  (void)setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &(int){60}, sizeof(int));
#endif
#ifdef TCP_KEEPINTVL
  (void)setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &(int){10}, sizeof(int));
#endif
#ifdef TCP_KEEPCNT
  (void)setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &(int){3}, sizeof(int));
#endif
#endif
  return 0;
}

static void
mc_close_fd(int *fd) {
  if(*fd >= 0) {
    close(*fd);
    *fd = -1;
  }
}

static void
mc_end_xfer(ftp_mc_t *mc) {
  free(mc->ring);
  mc->ring = NULL;
  mc->ring_size = 0;
  mc->xfer = 0;
}

void
ftp_mc_close_set(ftp_env_t *env) {
  ftp_mc_t *mc = env->mc;

  if(!mc) {
    return;
  }
  for(unsigned k = 0; k < mc->n; k++) {
    mc_close_fd(&mc->conn[k].fd);
    mc_close_fd(&mc->conn[k].lfd);
  }
  mc_end_xfer(mc);
  mc->open = 0;
  mc->kind = MC_NONE;
}

void
ftp_mc_free(ftp_env_t *env) {
  ftp_mc_close_set(env);
  free(env->mc);
  env->mc = NULL;
}

static ftp_mc_t *
mc_alloc(unsigned n, uint32_t stripe) {
  size_t size = sizeof(ftp_mc_t) + n * sizeof(mc_conn_t) +
                (n + 1) * sizeof(struct pollfd);
  ftp_mc_t *mc = calloc(1, size);

  if(!mc) {
    return NULL;
  }
  mc->n = n;
  mc->stripe = stripe;
  mc->pfd = (struct pollfd *)(void *)&mc->conn[n];
  for(unsigned k = 0; k < n; k++) {
    mc->conn[k].fd = -1;
    mc->conn[k].lfd = -1;
  }
  return mc;
}


/**
 * OPTS MC <N> [<S>] | OFF
 **/
static int
mc_parse_uint(const char **p, unsigned maxdigits, uint64_t *out) {
  const char *s = *p;
  uint64_t v = 0;
  unsigned i = 0;

  while(s[i] >= '0' && s[i] <= '9') {
    if(i == maxdigits) {
      return -1;
    }
    v = v * 10 + (uint64_t)(s[i] - '0');
    i++;
  }
  if(!i) {
    return -1;
  }
  *p = s + i;
  *out = v;
  return 0;
}

int
ftp_mc_opts(ftp_env_t *env, const char *arg) {
  uint64_t n;
  uint64_t stripe = env->mc ? env->mc->stripe : MC_DEFAULT_STRIPE;
  ftp_mc_t *mc;

  arg += strspn(arg, " ");
  if(!strcasecmp(arg, "OFF")) {
    ftp_mc_free(env);
    return ftp_active_printf(env, "200 MC OFF\r\n");
  }
  if(mc_parse_uint(&arg, 5, &n) || !n) {
    goto usage;
  }
  if(*arg == ' ') {
    arg += strspn(arg, " ");
    if(mc_parse_uint(&arg, 10, &stripe) || !stripe ||
       stripe > UINT32_MAX) {
      goto usage;
    }
  }
  if(*arg) {
    goto usage;
  }

  if(n > mc_max_conn) {
    n = mc_max_conn;
  }
  if(stripe > mc_stripe_limit((unsigned)n)) {
    stripe = mc_stripe_limit((unsigned)n);
  }
  if(stripe < FTP_MC_MIN_STRIPE) {
    stripe = FTP_MC_MIN_STRIPE;
  }

  if(!(mc = mc_alloc((unsigned)n, (uint32_t)stripe))) {
    return ftp_perror(env);
  }
  ftp_mc_free(env);
  env->mc = mc;
  return ftp_active_printf(env, "200 MC %u %u\r\n", mc->n, mc->stripe);

usage:
  return ftp_active_printf(env, "501 Usage: OPTS MC <N> [<S>] | OFF\r\n");
}


/**
 * Discard the single data connection settings and any old set.
 **/
static void
mc_reset_data(ftp_env_t *env) {
  ftp_data_close(env);
  if(env->passive_fd >= 0) {
    close(env->passive_fd);
    env->passive_fd = -1;
  }
  env->data_addr.sin_port = 0;
  env->data_addr.sin_addr.s_addr = 0;
}

static int
mc_listen(ftp_mc_t *mc, mc_conn_t *c) {
  struct sockaddr_in sa;
  socklen_t len = sizeof(sa);
  int flags;

  if((c->lfd = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
    return -1;
  }
  /* Accepted sockets inherit the window scale chosen here. */
  if(io_set_socket_buffers_size(c->lfd, mc_sock_bufsize(mc)) < 0 ||
     setsockopt(c->lfd, SOL_SOCKET, SO_REUSEADDR, &(int){1},
                sizeof(int)) < 0) {
    return -1;
  }
  memset(&sa, 0, sizeof(sa));
  sa.sin_family = AF_INET;
  sa.sin_addr.s_addr = htonl(INADDR_ANY);
  if(bind(c->lfd, (struct sockaddr *)&sa, sizeof(sa)) ||
     listen(c->lfd, 1) ||
     getsockname(c->lfd, (struct sockaddr *)&sa, &len)) {
    return -1;
  }
  flags = fcntl(c->lfd, F_GETFL, 0);
  if(flags < 0 || fcntl(c->lfd, F_SETFL, flags | O_NONBLOCK) < 0) {
    return -1;
  }
  c->port = ntohs(sa.sin_port);
  return 0;
}

int
ftp_cmd_MPSV(ftp_env_t *env, const char *arg) {
  char reply[64 + 6 * 256];
  ftp_mc_t *mc = env->mc;
  size_t len;

  if(arg[0]) {
    return ftp_active_printf(env, "501 Usage: MPSV\r\n");
  }
  if(!mc) {
    return ftp_active_printf(env, "503 Use OPTS MC first\r\n");
  }
  mc_reset_data(env);

  len = (size_t)snprintf(reply, sizeof(reply),
                         "224 Entering Multi-Passive Mode (|||");
  for(unsigned k = 0; k < mc->n; k++) {
    if(mc_listen(mc, &mc->conn[k])) {
      int err = errno;
      ftp_mc_close_set(env);
      errno = err;
      return ftp_perror(env);
    }
    len += (size_t)snprintf(reply + len, sizeof(reply) - len, "%s%u",
                            k ? "," : "", mc->conn[k].port);
  }
  mc->kind = MC_PASSIVE;
  return ftp_active_printf(env, "%s|)\r\n", reply);
}

int
ftp_cmd_MPRT(ftp_env_t *env, const char *arg) {
  struct sockaddr_in ctrl;
  socklen_t ctrl_len = sizeof(ctrl);
  char addrbuf[INET_ADDRSTRLEN];
  uint16_t ports[256];
  struct in_addr addr;
  ftp_mc_t *mc = env->mc;
  const char *p1, *p2, *p3, *p;
  unsigned count = 0;
  int low = 0;
  char d = arg[0];

  if(!mc) {
    return ftp_active_printf(env, "503 Use OPTS MC first\r\n");
  }
  if(d < 33 || d > 126 || (d >= '0' && d <= '9') || d == ',' || d == '.') {
    goto usage;
  }
  p1 = strchr(arg + 1, d);
  p2 = p1 ? strchr(p1 + 1, d) : NULL;
  p3 = p2 ? strchr(p2 + 1, d) : NULL;
  if(!p3 || p3[1]) {
    goto usage;
  }
  if(p1 != arg + 2 || arg[1] != '1') {
    return ftp_active_printf(env, "522 Network protocol not supported, use (1)\r\n");
  }
  if(p2 - p1 - 1 <= 0 || (size_t)(p2 - p1 - 1) >= sizeof(addrbuf)) {
    goto usage;
  }
  memcpy(addrbuf, p1 + 1, (size_t)(p2 - p1 - 1));
  addrbuf[p2 - p1 - 1] = '\0';
  if(inet_pton(AF_INET, addrbuf, &addr) != 1) {
    goto usage;
  }

  for(p = p2 + 1; p < p3; ) {
    uint64_t port;
    if(count == mc->n || mc_parse_uint(&p, 5, &port) || !port ||
       port > 65535 || (*p != ',' && p != p3)) {
      goto usage;
    }
    for(unsigned i = 0; i < count; i++) {
      if(ports[i] == port) {
        goto usage;
      }
    }
    if(port < 1024) {
      low = 1;
    }
    ports[count++] = (uint16_t)port;
    if(*p == ',') {
      p++;
      if(p == p3) {
        goto usage;
      }
    }
  }
  if(count != mc->n) {
    goto usage;
  }

  if(getpeername(env->active_fd, (struct sockaddr *)&ctrl, &ctrl_len) ||
     ctrl.sin_family != AF_INET || ctrl.sin_addr.s_addr != addr.s_addr ||
     low) {
    return ftp_active_printf(env, "504 MPRT address or port not allowed\r\n");
  }

  mc_reset_data(env);
  for(unsigned k = 0; k < mc->n; k++) {
    mc->conn[k].port = ports[k];
  }
  mc->addr = addr;
  mc->kind = MC_ACTIVE;
  return ftp_active_printf(env, "200 MPRT command successful\r\n");

usage:
  return ftp_active_printf(env,
                           "501 Usage: MPRT <d>1<d><addr><d><port>,...<d> "
                           "with exactly %u distinct ports\r\n", mc->n);
}


/**
 * Accept one connection on every listener, from the control peer only.
 **/
static int
mc_accept_all(ftp_env_t *env, ftp_mc_t *mc) {
  struct sockaddr_in ctrl;
  socklen_t ctrl_len = sizeof(ctrl);
  struct timespec start;

  if(getpeername(env->active_fd, (struct sockaddr *)&ctrl, &ctrl_len)) {
    return -1;
  }
  clock_gettime(CLOCK_MONOTONIC, &start);

  for(;;) {
    nfds_t nfds = 1;

    for(unsigned k = 0; k < mc->n; k++) {
      mc_conn_t *c = &mc->conn[k];
      if(c->lfd < 0) {
        continue;
      }
      for(;;) {
        struct sockaddr_in peer;
        socklen_t peer_len = sizeof(peer);
        int fd = accept(c->lfd, (struct sockaddr *)&peer, &peer_len);
        if(fd < 0) {
          if(errno == EINTR || errno == ECONNABORTED) {
            continue;
          }
          if(errno == EAGAIN || errno == EWOULDBLOCK) {
            break;
          }
          return -1;
        }
        if(peer.sin_family != AF_INET ||
           peer.sin_addr.s_addr != ctrl.sin_addr.s_addr) {
          /* Not the client: drop it and keep the port for the client. */
          close(fd);
          continue;
        }
        if(io_set_socket_buffers_size(fd, mc_sock_bufsize(mc)) < 0 ||
           mc_sock_opts(fd) < 0) {
          close(fd);
          return -1;
        }
        c->fd = fd;
        mc_close_fd(&c->lfd);
        break;
      }
      if(c->lfd >= 0) {
        mc->pfd[nfds].fd = c->lfd;
        mc->pfd[nfds].events = POLLIN;
        mc->pfd[nfds].revents = 0;
        nfds++;
      }
    }
    if(nfds == 1) {
      return 0;
    }
    if(mc_poll(env, mc, nfds, &start, FTP_MC_CONNECT_TIMEOUT_SEC)) {
      return -1;
    }
  }
}

/**
 * Connect to every port of MPRT concurrently.
 **/
static int
mc_connect_all(ftp_env_t *env, ftp_mc_t *mc) {
  struct timespec start;

  for(unsigned k = 0; k < mc->n; k++) {
    mc_conn_t *c = &mc->conn[k];
    struct sockaddr_in sa;

    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr = mc->addr;
    sa.sin_port = htons(c->port);
    if((c->fd = socket(AF_INET, SOCK_STREAM, 0)) < 0 ||
       io_set_socket_buffers_size(c->fd, mc_sock_bufsize(mc)) < 0 ||
       mc_sock_opts(c->fd) < 0) {
      return -1;
    }
    if(connect(c->fd, (struct sockaddr *)&sa, sizeof(sa)) && errno != EINPROGRESS) {
      return -1;
    }
    c->done = 0;
  }

  clock_gettime(CLOCK_MONOTONIC, &start);
  for(;;) {
    nfds_t nfds = 1;

    for(unsigned k = 0; k < mc->n; k++) {
      if(!mc->conn[k].done) {
        mc->pfd[nfds].fd = mc->conn[k].fd;
        mc->pfd[nfds].events = POLLOUT;
        mc->pfd[nfds].revents = 0;
        nfds++;
      }
    }
    if(nfds == 1) {
      return 0;
    }
    if(mc_poll(env, mc, nfds, &start, FTP_MC_CONNECT_TIMEOUT_SEC)) {
      return -1;
    }
    for(nfds_t i = 1; i < nfds; i++) {
      int err = 0;
      socklen_t len = sizeof(err);
      if(!mc->pfd[i].revents) {
        continue;
      }
      if(getsockopt(mc->pfd[i].fd, SOL_SOCKET, SO_ERROR, &err, &len)) {
        return -1;
      }
      if(err) {
        errno = err;
        return -1;
      }
      for(unsigned k = 0; k < mc->n; k++) {
        if(mc->conn[k].fd == mc->pfd[i].fd) {
          mc->conn[k].done = 1;
        }
      }
    }
  }
}

/**
 * True when every listener already has a pending connection.
 **/
static int
mc_listeners_ready(ftp_mc_t *mc) {
  for(unsigned k = 0; k < mc->n; k++) {
    mc->pfd[k].fd = mc->conn[k].lfd;
    mc->pfd[k].events = POLLIN;
    mc->pfd[k].revents = 0;
  }
  if(poll(mc->pfd, mc->n, 0) != (int)mc->n) {
    return 0;
  }
  for(unsigned k = 0; k < mc->n; k++) {
    if(!(mc->pfd[k].revents & POLLIN)) {
      return 0;
    }
  }
  return 1;
}

int
ftp_mc_precheck(ftp_env_t *env) {
  ftp_mc_t *mc = env->mc;

  if(!mc->open) {
    return 0;
  }
  /* A persistent MODE B set is usable only if every connection is. */
  for(unsigned k = 0; k < mc->n; k++) {
    unsigned char byte;
    ssize_t n;
    do {
      n = recv(mc->conn[k].fd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
    } while(n < 0 && errno == EINTR);
    if(n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
      ftp_mc_close_set(env);
      return ftp_active_printf(env, "425 Data connection closed; "
                               "use MPSV or MPRT\r\n") < 0 ? -1 : 1;
    }
  }
  return 0;
}

int
ftp_mc_xfer_start(ftp_env_t *env, int prechecked) {
  ftp_mc_t *mc = env->mc;
  uint64_t ring = mc_ring_bytes(mc->n, mc->stripe);
  int reused = mc->open;
  int ready = 0;

  if(!prechecked) {
    int rc = ftp_mc_precheck(env);
    if(rc) {
      return rc;
    }
  }

  if(ring > SIZE_MAX / 2 || !(mc->ring = malloc((size_t)ring))) {
    return ftp_active_printf(env, "451 Insufficient memory for the transfer\r\n")
           < 0 ? -1 : 1;
  }
  mc->ring_size = (size_t)ring;
  mc->aborted = 0;
  mc->ctrl_lost = 0;
  mc->ctrl_watch = 1;

  if(!reused) {
    int rc;
    if(mc->kind == MC_ACTIVE) {
      if(ftp_active_printf(env, "150 Opening data connections (MC %u %u)\r\n",
                           mc->n, mc->stripe)) {
        ftp_mc_close_set(env);
        return -1;
      }
      rc = mc_connect_all(env, mc);
    } else {
      /* Draft Section 5.1: no preliminary reply before all N are open. */
      ready = mc_listeners_ready(mc);
      rc = mc_accept_all(env, mc);
    }
    if(rc) {
      int lost = mc->ctrl_lost;
      ftp_mc_close_set(env);
      if(lost) {
        return -1;
      }
      return ftp_active_printf(env, "425 Can't open data connections\r\n")
             < 0 ? -1 : 1;
    }
    mc->open = 1;
  }

  if(reused || mc->kind == MC_PASSIVE) {
    if(ftp_active_printf(env, reused || ready ?
                         "125 Data connections already open; "
                         "transfer starting (MC %u %u)\r\n" :
                         "150 Opening data connections (MC %u %u)\r\n",
                         mc->n, mc->stripe)) {
      ftp_mc_close_set(env);
      return -1;
    }
  }

  for(unsigned k = 0; k < mc->n; k++) {
    mc_conn_t *c = &mc->conn[k];
    c->done = 0;
    c->q = 0;
    c->hdr_len = 3;  /* sending: no header pending */
    c->blk_rem = 0;
    c->blk_eof = 0;
  }
  mc->head = 0;
  mc->end = UINT64_MAX;
  mc->len_known = 0;
  mc->flushed = 0;
  mc->xfer = 1;
  mc_touch(mc);
  return 0;
}


/**
 * Sending. The ring holds stream octets [low, head), where low is the
 * oldest octet a connection still has to send.
 **/
static size_t
mc_send_space(const ftp_mc_t *mc) {
  uint64_t low = mc->head;

  for(unsigned k = 0; k < mc->n; k++) {
    if(!mc->conn[k].done) {
      uint64_t x = mc_off(mc, k, mc->conn[k].q);
      if(x < low) {
        low = x;
      }
    }
  }
  return mc->ring_size - (size_t)(mc->head - low);
}

/**
 * Send what connection k can send now. Returns 1 when the socket is full,
 * 0 when the connection waits for data or is done, -1 on error.
 **/
static int
mc_send_conn(ftp_env_t *env, ftp_mc_t *mc, unsigned k) {
  mc_conn_t *c = &mc->conn[k];

  while(!c->done) {
    struct iovec iov[MC_IOV + 1];
    struct msghdr msg;
    uint64_t total = mc->len_known ? mc_qmax(mc, k, mc->end) : UINT64_MAX;
    size_t payload = 0;
    int cnt = 0;
    ssize_t r;

    if(env->mode == 'B') {
      if(c->hdr_len == 3 && !c->blk_rem) {
        uint64_t avail = mc_qmax(mc, k, mc->head) - c->q;
        int eof;
        if(avail > MC_BLOCK_MAX) {
          avail = MC_BLOCK_MAX;
        }
        if(!avail && c->q < total) {
          return 0;
        }
        eof = c->q + avail == total;
        c->hdr[0] = eof ? MC_BLOCK_EOF : 0;
        c->hdr[1] = (unsigned char)(avail >> 8);
        c->hdr[2] = (unsigned char)avail;
        c->hdr_len = 0;
        c->blk_rem = (size_t)avail;
        c->blk_eof = eof;
      }
      if(c->hdr_len < 3) {
        iov[cnt].iov_base = c->hdr + c->hdr_len;
        iov[cnt].iov_len = 3 - c->hdr_len;
        cnt++;
      }
      cnt += mc_iov(mc, k, c->q, mc->head, c->blk_rem, iov + cnt, MC_IOV,
                    &payload);
    } else {
      if(c->q == total) {
        mc_close_fd(&c->fd);
        c->done = 1;
        return 0;
      }
      cnt = mc_iov(mc, k, c->q, mc->head, MC_SEND_MAX, iov, MC_IOV, &payload);
      if(!cnt) {
        return 0;
      }
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = iov;
    msg.msg_iovlen = cnt;
    r = sendmsg(c->fd, &msg, MC_SEND_FLAGS);
    if(r < 0) {
      if(errno == EINTR) {
        continue;
      }
      if(errno == EAGAIN || errno == EWOULDBLOCK) {
        return 1;
      }
      return -1;
    }
    mc_touch(mc);

    size_t n = (size_t)r;
    if(env->mode == 'B') {
      if(c->hdr_len < 3) {
        size_t h = 3 - c->hdr_len < n ? 3 - c->hdr_len : n;
        c->hdr_len += (unsigned)h;
        n -= h;
      }
      c->blk_rem -= n;
      c->q += n;
      if(c->hdr_len == 3 && !c->blk_rem && c->blk_eof) {
        c->done = 1;
      }
    } else {
      c->q += n;
    }
  }
  return 0;
}

/**
 * Send on every connection until all are done (all), or until the ring
 * has need octets of free space.
 **/
static int
mc_send_wait(ftp_env_t *env, ftp_mc_t *mc, size_t need, int all) {
  for(;;) {
    nfds_t nfds = 1;
    int all_done = 1;

    for(unsigned k = 0; k < mc->n; k++) {
      int rc = mc_send_conn(env, mc, k);
      if(rc < 0) {
        return -1;
      }
      if(!mc->conn[k].done) {
        all_done = 0;
      }
      if(rc == 1) {
        mc->pfd[nfds].fd = mc->conn[k].fd;
        mc->pfd[nfds].events = POLLOUT;
        mc->pfd[nfds].revents = 0;
        nfds++;
      }
    }
    if(all ? all_done : mc_send_space(mc) >= need) {
      return 0;
    }
    if(nfds == 1) {
      errno = EIO;
      return -1;
    }
    if(mc_poll(env, mc, nfds, &mc->progress, FTP_MC_STALL_TIMEOUT_SEC)) {
      return -1;
    }
  }
}

/**
 * Copy into the ring at head; the caller checked the free space.
 **/
static void
mc_ring_put(ftp_mc_t *mc, const void *buf, size_t len) {
  size_t idx = (size_t)(mc->head % mc->ring_size);
  size_t first = mc->ring_size - idx < len ? mc->ring_size - idx : len;

  memcpy(mc->ring + idx, buf, first);
  memcpy(mc->ring, (const char *)buf + first, len - first);
  mc->head += len;
}

int
ftp_mc_write(ftp_env_t *env, const void *buf, size_t count) {
  ftp_mc_t *mc = env->mc;
  const char *p = buf;

  while(count) {
    size_t space = mc_send_space(mc);
    if(!space) {
      size_t need = mc->ring_size / 4 < count ? mc->ring_size / 4 : count;
      if(mc_send_wait(env, mc, need, 0)) {
        return -1;
      }
      continue;
    }
    if(space > count) {
      space = count;
    }
    mc_ring_put(mc, p, space);
    p += space;
    count -= space;
    if(mc_send_wait(env, mc, 0, 0)) {
      return -1;
    }
  }
  return 0;
}

static int
mc_finish_ok(ftp_env_t *env, ftp_mc_t *mc) {
  mc_end_xfer(mc);
  if(env->mode == 'B') {
    return ftp_active_printf(env, "250 Transfer complete; data connections kept open\r\n");
  }
  ftp_mc_close_set(env);
  return ftp_active_printf(env, "226 Transfer complete\r\n");
}

int
ftp_mc_send_file(ftp_env_t *env, int fd, off_t off, uint64_t size) {
  ftp_mc_t *mc = env->mc;
  size_t seg = mc->ring_size / 4;

  mc->end = size;
  mc->len_known = 1;
  while(mc->head < size) {
    uint64_t left = size - mc->head;
    size_t want = left < seg ? (size_t)left : seg;
    size_t space = mc_send_space(mc);
    size_t idx, first;

    if(space < want) {
      if(mc_send_wait(env, mc, want, 0)) {
        return ftp_mc_xfer_fail(env);
      }
      continue;
    }
    /* Large sequential reads; the sockets keep sending meanwhile. */
    idx = (size_t)(mc->head % mc->ring_size);
    first = mc->ring_size - idx < want ? mc->ring_size - idx : want;
    if(io_pread(fd, mc->ring + idx, first, off + (off_t)mc->head) ||
       (want > first &&
        io_pread(fd, mc->ring, want - first, off + (off_t)(mc->head + first)))) {
      return ftp_mc_xfer_fail(env);
    }
    mc->head += want;
    if(mc_send_wait(env, mc, 0, 0)) {
      return ftp_mc_xfer_fail(env);
    }
  }
  if(mc_send_wait(env, mc, 0, 1)) {
    return ftp_mc_xfer_fail(env);
  }
  return mc_finish_ok(env, mc);
}

int
ftp_mc_xfer_finish(ftp_env_t *env, int sending) {
  ftp_mc_t *mc = env->mc;

  if(sending) {
    mc->end = mc->head;
    mc->len_known = 1;
    if(mc_send_wait(env, mc, 0, 1)) {
      return ftp_mc_xfer_fail(env);
    }
  }
  return mc_finish_ok(env, mc);
}

int
ftp_mc_xfer_fail(ftp_env_t *env) {
  ftp_mc_t *mc = env->mc;
  int err = errno;
  int rc;

  if(!mc) {
    return ftp_perror(env);
  }
  ftp_mc_close_set(env);
  if(mc->ctrl_lost) {
    return -1;
  }
  if(mc->aborted) {
    rc = ftp_active_printf(env, "426 Transfer aborted\r\n");
  } else if(err == EPIPE || err == ECONNRESET) {
    rc = ftp_active_printf(env, "426 Data connection closed\r\n");
  } else if(err == ETIMEDOUT || err == EAGAIN) {
    rc = ftp_active_printf(env, "426 Data connection timed out\r\n");
  } else if(err == EPROTO) {
    rc = ftp_active_printf(env, "426 Invalid data on the connection set\r\n");
  } else {
    errno = err;
    rc = ftp_perror(env);
  }
  return rc < 0 ? -1 : 0;
}


/**
 * Receiving. Connections write their stripes into the ring at their
 * stream offsets, below flushed + ring size; the contiguous prefix is
 * written to the file in order, so the file always holds a valid prefix.
 **/
static uint64_t
mc_recv_prefix(const ftp_mc_t *mc) {
  uint64_t p = mc->end;

  for(unsigned k = 0; k < mc->n; k++) {
    uint64_t x = mc_off(mc, k, mc->conn[k].q);
    if(x < p) {
      p = x;
    }
  }
  return p;
}

static int
mc_flush(ftp_mc_t *mc, int fd, uint64_t upto) {
  while(mc->flushed < upto) {
    size_t idx = (size_t)(mc->flushed % mc->ring_size);
    uint64_t len = upto - mc->flushed;
    if(len > mc->ring_size - idx) {
      len = mc->ring_size - idx;
    }
    if(io_nwrite(fd, mc->ring + idx, (size_t)len)) {
      return -1;
    }
    mc->flushed += len;
  }
  return 0;
}

/**
 * A connection ended at stream offset e. The data stream cannot extend
 * past the smallest end, and no connection may hold octets beyond it.
 **/
static int
mc_recv_ended(ftp_mc_t *mc, uint64_t e) {
  if(e < mc->end) {
    mc->end = e;
    for(unsigned k = 0; k < mc->n; k++) {
      if(mc->conn[k].q > mc_qmax(mc, k, e)) {
        errno = EPROTO;
        return -1;
      }
    }
  }
  return 0;
}

/**
 * Receive what connection k has. Returns 1 when the socket is empty, 2
 * when the ring has no room for its next octet, 0 when done, -1 on error.
 **/
static int
mc_recv_conn(ftp_env_t *env, ftp_mc_t *mc, unsigned k) {
  mc_conn_t *c = &mc->conn[k];

  while(!c->done) {
    struct iovec iov[MC_IOV + 1];
    uint64_t lim_ring = mc->flushed + mc->ring_size;
    uint64_t lim = lim_ring < mc->end ? lim_ring : mc->end;
    uint64_t x = mc_off(mc, k, c->q);
    unsigned char probe;
    size_t payload = 0;
    int cnt = 0;
    ssize_t r;

    if(env->mode == 'B' && !c->blk_rem) {
      iov[0].iov_base = c->hdr + c->hdr_len;
      iov[0].iov_len = 3 - c->hdr_len;
      cnt = 1;
    } else if(x >= mc->end) {
      if(env->mode == 'B') {
        errno = EPROTO;
        return -1;
      }
      /* Past the end the only valid event is end-of-data. */
      iov[0].iov_base = &probe;
      iov[0].iov_len = 1;
      cnt = 1;
    } else if(x >= lim_ring) {
      return 2;
    } else if(env->mode == 'B') {
      cnt = mc_iov(mc, k, c->q, lim, c->blk_rem, iov, MC_IOV, &payload);
      if(payload == c->blk_rem && !c->blk_eof) {
        iov[cnt].iov_base = c->hdr;
        iov[cnt].iov_len = 3;
        cnt++;
      }
    } else {
      cnt = mc_iov(mc, k, c->q, lim, MC_SEND_MAX, iov, MC_IOV, &payload);
    }

    r = readv(c->fd, iov, cnt);
    if(r < 0) {
      if(errno == EINTR) {
        continue;
      }
      if(errno == EAGAIN || errno == EWOULDBLOCK) {
        return 1;
      }
      return -1;
    }
    if(!r) {
      if(env->mode == 'B') {
        errno = EPIPE;  /* closed without an EOF block */
        return -1;
      }
      mc_close_fd(&c->fd);
      c->done = 1;
      return mc_recv_ended(mc, x);
    }
    mc_touch(mc);

    size_t n = (size_t)r;
    if(env->mode != 'B') {
      if(x >= mc->end) {
        errno = EPROTO;
        return -1;
      }
      c->q += n;
      continue;
    }

    size_t p = n < payload ? n : payload;
    c->q += p;
    c->blk_rem -= p;
    c->hdr_len += (unsigned)(n - p);
    if(c->hdr_len == 3) {
      c->hdr_len = 0;
      /* Draft Section 7.2: only the EOF bit is allowed on a set. */
      if(c->hdr[0] & ~MC_BLOCK_EOF) {
        errno = EPROTO;
        return -1;
      }
      c->blk_rem = ((size_t)c->hdr[1] << 8) | c->hdr[2];
      c->blk_eof = !!(c->hdr[0] & MC_BLOCK_EOF);
    }
    if(!c->blk_rem && c->blk_eof && !c->hdr_len) {
      c->done = 1;
      return mc_recv_ended(mc, mc_off(mc, k, c->q));
    }
  }
  return 0;
}

int
ftp_mc_recv_file(ftp_env_t *env, int fd, uint64_t *written) {
  ftp_mc_t *mc = env->mc;
  size_t seg = mc->ring_size / 4;
  int err;

  for(unsigned k = 0; k < mc->n; k++) {
    mc->conn[k].hdr_len = 0;  /* receiving: a header is expected */
  }

  for(;;) {
    uint64_t before = mc->flushed;
    nfds_t nfds = 1;
    int all_done = 1;
    int blocked = 0;
    uint64_t prefix;

    for(unsigned k = 0; k < mc->n; k++) {
      int rc = mc_recv_conn(env, mc, k);
      if(rc < 0) {
        goto fail;
      }
      if(rc == 1) {
        mc->pfd[nfds].fd = mc->conn[k].fd;
        mc->pfd[nfds].events = POLLIN;
        mc->pfd[nfds].revents = 0;
        nfds++;
      } else if(rc == 2) {
        blocked = 1;
      }
    }
    for(unsigned k = 0; k < mc->n; k++) {
      if(!mc->conn[k].done) {
        all_done = 0;
      }
    }

    prefix = mc_recv_prefix(mc);
    if(prefix > mc->flushed &&
       (all_done || blocked || prefix - mc->flushed >= seg)) {
      if(mc_flush(mc, fd, prefix)) {
        *written = mc->flushed;
        return -1;
      }
    }
    if(all_done) {
      break;
    }
    if(nfds == 1) {
      if(blocked && mc->flushed > before) {
        continue;
      }
      errno = EIO;
      goto fail;
    }
    if(mc_poll(env, mc, nfds, &mc->progress, FTP_MC_STALL_TIMEOUT_SEC)) {
      goto fail;
    }
  }

  /* Every count must be b(k) for the length found (draft Section 6.4). */
  for(unsigned k = 0; k < mc->n; k++) {
    if(mc->conn[k].q != mc_qmax(mc, k, mc->end)) {
      errno = EPROTO;
      goto fail;
    }
  }
  *written = mc->flushed;
  return 0;

fail:
  /* Keep the valid prefix that arrived (draft Section 8.3). */
  err = errno;
  (void)mc_flush(mc, fd, mc_recv_prefix(mc));
  *written = mc->flushed;
  errno = err;
  return -1;
}
