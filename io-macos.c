/* macOS sendfile has a different signature from Linux and FreeBSD. */
#include <errno.h>
#include <sys/socket.h>

#include "io.h"

int
io_sendfile(int fd, int sock, off_t offset, size_t n) {
  while(n) {
    off_t sent = (off_t)(n < IO_COPY_BUFSIZE ? n : IO_COPY_BUFSIZE);
    int rc = sendfile(fd, sock, offset, &sent, NULL, 0);
    if(sent > 0) {
      offset += sent;
      n -= (size_t)sent;
    }
    if(!rc) {
      if(!sent) {
        errno = EIO;
        return -1;
      }
      continue;
    }
    if(errno == EINTR || (sent > 0 && (errno == EAGAIN || errno == EWOULDBLOCK))) {
      continue;
    }
    return -1;
  }
  return 0;
}
