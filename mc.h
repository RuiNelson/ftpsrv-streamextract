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

/**
 * FTPMC: one transfer over several data connections (draft-ftpmc-00).
 *
 * A transfer on a connection set runs in the session thread with
 * non-blocking sockets and poll(). The data stream goes through one ring
 * buffer in stream order: each connection reads or writes its own stripes
 * of the ring with scatter/gather I/O, and the file is read or written
 * with large sequential calls. The ring exists only during a transfer, so
 * an idle session, even with a persistent MODE B set, holds no buffer.
 **/

#pragma once

#include <stdint.h>
#include <sys/types.h>

#include "cmd.h"

/* Largest N the server accepts (the FEAT value). */
#ifndef FTP_MC_MAX_CONN
#define FTP_MC_MAX_CONN 16
#endif

/* Largest and smallest stripe sizes the server accepts. */
#ifndef FTP_MC_MAX_STRIPE
#define FTP_MC_MAX_STRIPE (64 * 1024)
#endif
#ifndef FTP_MC_MIN_STRIPE
#define FTP_MC_MIN_STRIPE 512
#endif

/**
 * Change the limits at startup. Returns -1 if they are invalid: max_conn
 * must be 1-256, the stripe range must include the default of 4096, and
 * max_conn connections with that stripe must fit the buffer budget
 * (FTP_MC_BUFFER_BUDGET, 16 MiB), which allows up to 60 connections.
 **/
int ftp_mc_configure(unsigned max_conn, uint32_t max_stripe);

/**
 * The value announced in the FEAT line " MC <max-conn>".
 **/
unsigned ftp_mc_max_conn(void);

/**
 * Minimum ring size and buffer budget of a transfer, in octets.
 **/
size_t ftp_mc_ring_size(void);
size_t ftp_mc_buffer_budget(void);

/**
 * Commands: OPTS MC (arg is the text after "MC"), MPSV and MPRT.
 **/
int ftp_mc_opts(ftp_env_t *env, const char *arg);
int ftp_cmd_MPSV(ftp_env_t *env, const char *arg);
int ftp_cmd_MPRT(ftp_env_t *env, const char *arg);

/**
 * True when the next transfer uses a connection set.
 **/
int ftp_mc_has_set(const ftp_env_t *env);

/**
 * True while a transfer on a connection set is in progress.
 **/
int ftp_mc_xfer_active(const ftp_env_t *env);

/**
 * Same contract as ftp_data_precheck(): 0 to proceed, 1 if an error was
 * reported, -1 if the control connection failed.
 **/
int ftp_mc_precheck(ftp_env_t *env);

/**
 * Same contract as ftp_data_xfer_start().
 **/
int ftp_mc_xfer_start(ftp_env_t *env, int prechecked);

/**
 * Append data to the outgoing data stream (listings, generated files).
 * Returns -1 with errno set on failure, without replying.
 **/
int ftp_mc_write(ftp_env_t *env, const void *buf, size_t count);

/**
 * Send SIZE octets of FD starting at offset OFF, then finish the
 * transfer and reply. Returns -1 if the control connection failed.
 **/
int ftp_mc_send_file(ftp_env_t *env, int fd, off_t off, uint64_t size);

/**
 * Receive the data stream and write it in order to FD at its current
 * position. *WRITTEN is the number of octets written, also on failure,
 * when they are a valid prefix of the data stream. Returns -1 with errno
 * set on failure, without replying: the caller truncates the file and
 * then calls ftp_mc_xfer_fail().
 **/
int ftp_mc_recv_file(ftp_env_t *env, int fd, uint64_t *written);

/**
 * Complete a successful transfer (sending: flush and end the data
 * stream first) and reply 226, or 250 when a MODE B set stays open.
 **/
int ftp_mc_xfer_finish(ftp_env_t *env, int sending);

/**
 * Discard the connection set after a failed transfer and reply for the
 * error in errno. Returns -1 if the control connection failed.
 **/
int ftp_mc_xfer_fail(ftp_env_t *env);

/**
 * True once if ABOR interrupted a transfer, which already got its 426;
 * ABOR then replies 226.
 **/
int ftp_mc_take_abort(ftp_env_t *env);

/**
 * Close every connection and listener of the set, and end any transfer.
 **/
void ftp_mc_close_set(ftp_env_t *env);

/**
 * Release all MC state (OPTS MC OFF, end of session).
 **/
void ftp_mc_free(ftp_env_t *env);
