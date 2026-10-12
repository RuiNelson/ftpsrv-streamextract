/* Minimal ELF64 metadata for hosts, such as macOS, without <elf.h>. */
#pragma once

#if defined(__APPLE__)
#include <stdint.h>

typedef struct {
  unsigned char e_ident[16];
  uint16_t e_type;
  uint16_t e_machine;
  uint32_t e_version;
  uint64_t e_entry;
  uint64_t e_phoff;
  uint64_t e_shoff;
  uint32_t e_flags;
  uint16_t e_ehsize;
  uint16_t e_phentsize;
  uint16_t e_phnum;
  uint16_t e_shentsize;
  uint16_t e_shnum;
  uint16_t e_shstrndx;
} Elf64_Ehdr;

typedef struct {
  uint32_t p_type;
  uint32_t p_flags;
  uint64_t p_offset;
  uint64_t p_vaddr;
  uint64_t p_paddr;
  uint64_t p_filesz;
  uint64_t p_memsz;
  uint64_t p_align;
} Elf64_Phdr;

_Static_assert(sizeof(Elf64_Ehdr) == 64, "ELF64 file header size");
_Static_assert(sizeof(Elf64_Phdr) == 56, "ELF64 program header size");
#else
#include <elf.h>
#endif
