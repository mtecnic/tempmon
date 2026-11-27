// gddr6.c
#define _GNU_SOURCE

#include "gddr6.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <pci/pci.h>
#include <signal.h>

#define PG_SZ sysconf(_SC_PAGE_SIZE)
#define PRINT_ERROR()                                          \
      do {                                                     \
      fprintf(stderr, "Error at line %d, file %s (%d) [%s]\n", \
      __LINE__, __FILE__, errno, strerror(errno)); exit(1);    \
      } while(0)

#define MAX_DEVICES 32

/* GDDR7 per-module layout (GB202), from olealgoritme/gpuwatch */
#define GDDR7_MODULE_STRIDE 0x4000
#define GDDR7_VALID_OFF     0x10
#define GDDR7_MAX_MODULES   16

struct gddr6_ctx ctx = {0};
struct device dev_table[] =
{
    { .offset = 0x0000E2A8, .dev_id = 0x2684, .vram = "GDDR6X", .arch = "AD102", .name =  "RTX 4090" },
    { .offset = 0x0000E2A8, .dev_id = 0x2685, .vram = "GDDR6X", .arch = "AD102", .name =  "RTX 4090 D" },
    { .offset = 0x0000E2A8, .dev_id = 0x2702, .vram = "GDDR6X", .arch = "AD103", .name =  "RTX 4080 Super" },
    { .offset = 0x0000E2A8, .dev_id = 0x2704, .vram = "GDDR6X", .arch = "AD103", .name =  "RTX 4080" },
    { .offset = 0x0000E2A8, .dev_id = 0x2705, .vram = "GDDR6X", .arch = "AD103", .name =  "RTX 4070 Ti Super" },
    { .offset = 0x0000E2A8, .dev_id = 0x2782, .vram = "GDDR6X", .arch = "AD104", .name =  "RTX 4070 Ti" },
    { .offset = 0x0000E2A8, .dev_id = 0x2783, .vram = "GDDR6X", .arch = "AD104", .name =  "RTX 4070 Super" },
    { .offset = 0x0000E2A8, .dev_id = 0x2786, .vram = "GDDR6X", .arch = "AD104", .name =  "RTX 4070" },
    { .offset = 0x0000E2A8, .dev_id = 0x2860, .vram = "GDDR6",  .arch = "AD106", .name =  "RTX 4070 Max-Q / Mobile" },
    { .offset = 0x0000E2A8, .dev_id = 0x2203, .vram = "GDDR6X", .arch = "GA102", .name =  "RTX 3090 Ti" },
    { .offset = 0x0000E2A8, .dev_id = 0x2204, .vram = "GDDR6X", .arch = "GA102", .name =  "RTX 3090" },
    { .offset = 0x0000E2A8, .dev_id = 0x2208, .vram = "GDDR6X", .arch = "GA102", .name =  "RTX 3080 Ti" },
    { .offset = 0x0000E2A8, .dev_id = 0x2206, .vram = "GDDR6X", .arch = "GA102", .name =  "RTX 3080" },
    { .offset = 0x0000E2A8, .dev_id = 0x2216, .vram = "GDDR6X", .arch = "GA102", .name =  "RTX 3080 LHR" },
    { .offset = 0x0000EE50, .dev_id = 0x2484, .vram = "GDDR6",  .arch = "GA104", .name =  "RTX 3070" },
    { .offset = 0x0000EE50, .dev_id = 0x2488, .vram = "GDDR6",  .arch = "GA104", .name =  "RTX 3070 LHR" },
    { .offset = 0x0000E2A8, .dev_id = 0x2531, .vram = "GDDR6",  .arch = "GA106", .name =  "RTX A2000" },
    { .offset = 0x0000E2A8, .dev_id = 0x2571, .vram = "GDDR6",  .arch = "GA106", .name =  "RTX A2000" },
    { .offset = 0x0000E2A8, .dev_id = 0x2232, .vram = "GDDR6",  .arch = "GA102", .name =  "RTX A4500" },
    { .offset = 0x0000E2A8, .dev_id = 0x2231, .vram = "GDDR6",  .arch = "GA102", .name =  "RTX A5000" },
    { .offset = 0x0000E2A8, .dev_id = 0x26B1, .vram = "GDDR6",  .arch = "AD102", .name =  "RTX A6000" },
    { .offset = 0x0000E2A8, .dev_id = 0x27b8, .vram = "GDDR6",  .arch = "AD104", .name =  "L4" },
    { .offset = 0x0000E2A8, .dev_id = 0x26b9, .vram = "GDDR6",  .arch = "AD102", .name =  "L40S" },
    { .offset = 0x0000E2A8, .dev_id = 0x2236, .vram = "GDDR6",  .arch = "GA102", .name =  "A10" },
    /* Blackwell GDDR7: per-module FBPA DRAM sensors, reverse-engineered by
     * olealgoritme/gpuwatch (MIT). See gddr7_hottest_module() below.
     * Only 0x2B85 is tested; the rest share the GB202 die (IDs from pci.ids)
     * and are assumed to share its FBPA register layout. */
    { .offset = 0x009024C0, .dev_id = 0x2B85, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX 5090", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2B87, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX 5090 D", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2B8C, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX 5090 D V2", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BB1, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 6000 Blackwell", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BB2, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 6000D Blackwell", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BB3, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 5000 Blackwell", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BB4, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 6000 Blackwell Max-Q", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BB5, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 6000 Blackwell Server", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BB9, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX 6000D", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BBC, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 6000D Blackwell Max-Q", .kind = VRAM_GDDR7 },
    { .offset = 0x009024C0, .dev_id = 0x2BBD, .vram = "GDDR7",  .arch = "GB202", .name =  "RTX PRO 5500 Blackwell", .kind = VRAM_GDDR7 },
};

void gddr6_init(void)
{
    ctx.fd = open("/dev/mem", O_RDONLY);
    if (ctx.fd == -1) {
        PRINT_ERROR();
    }
}

int gddr6_detect_compatible_gpus(void)
{
    ctx.devices = NULL;
    ctx.num_devices = 0;

    struct pci_access *pacc = NULL;
    struct pci_dev *pci_dev = NULL;
    ssize_t dev_table_size = (sizeof(dev_table)/sizeof(struct device));

    pacc = pci_alloc();
    pci_init(pacc);
    pci_scan_bus(pacc);

    for (pci_dev = pacc->devices; pci_dev != NULL; pci_dev = pci_dev->next) 
    {
          pci_fill_info(pci_dev, PCI_FILL_IDENT | PCI_FILL_BASES | PCI_FILL_CLASS);
          for (uint32_t i = 0; i < dev_table_size; ++i) 
          {
              if (pci_dev->device_id == dev_table[i].dev_id) 
              {
                  struct device *new_devices = realloc(ctx.devices, (ctx.num_devices + 1) * sizeof(struct device));
                  if (new_devices == NULL)
                  {
                      fprintf(stderr, "Memory allocation failed\n");
                      pci_cleanup(pacc);
                      free(ctx.devices);
                      ctx.devices = NULL;
                      return 0;
                  }
                  ctx.devices = new_devices;

                  ctx.devices[ctx.num_devices] = dev_table[i];
                  ctx.devices[ctx.num_devices].bar0 = (pci_dev->base_addr[0] & 0xffffffff);
                  ctx.devices[ctx.num_devices].bus = pci_dev->bus;
                  ctx.devices[ctx.num_devices].dev = pci_dev->dev;
                  ctx.devices[ctx.num_devices].func = pci_dev->func;
                  ctx.num_devices++;
              }
          }
      }

    pci_cleanup(pacc);
    return ctx.num_devices;
}

void gddr6_memory_map(void)
{
    for (uint32_t i = 0; i < ctx.num_devices; i++)
    {
        ctx.devices[i].phys_addr = (ctx.devices[i].bar0 + ctx.devices[i].offset);
        ctx.devices[i].base_offset = ctx.devices[i].phys_addr & ~(PG_SZ - 1);

        size_t len = PG_SZ;
        if (ctx.devices[i].kind == VRAM_GDDR7)
        {
            size_t span = (ctx.devices[i].phys_addr - ctx.devices[i].base_offset)
                        + (GDDR7_MAX_MODULES - 1) * GDDR7_MODULE_STRIDE + GDDR7_VALID_OFF + sizeof(uint32_t);
            len = (span + PG_SZ - 1) & ~(PG_SZ - 1);
        }
        ctx.devices[i].map_len = len;

        ctx.devices[i].mapped_addr = mmap(0, len, PROT_READ, MAP_SHARED, ctx.fd, ctx.devices[i].base_offset);
        if (ctx.devices[i].mapped_addr == MAP_FAILED)
        {
            ctx.devices[i].mapped_addr = NULL;
            fprintf(stderr, "Memory mapping failed for pci=%x:%x:%x\n", ctx.devices[i].bus, ctx.devices[i].dev, ctx.devices[i].func);
            fprintf(stderr, "Did you enable iomem=relaxed? Are you r00t?\n");
            exit(EXIT_FAILURE);
        } else {
            printf("Device: %s %s (%s / 0x%04x) pci=%x:%x:%x\n", ctx.devices[i].name, ctx.devices[i].vram,
            ctx.devices[i].arch, ctx.devices[i].dev_id, ctx.devices[i].bus, ctx.devices[i].dev, ctx.devices[i].func);
        }
    }
}

/* Hottest valid GDDR7 module in degrees C, or INT32_MIN if none read valid.
 * Bits 23:16 of each FBPA DQR register hold a GDDR mode-register temperature
 * code; a module is present when bits 27:24 of the register at +0x10 are all
 * set, and 0xBADFxxxx is the GPU's "no such register" poison value. */
static int gddr7_hottest_module(const volatile uint8_t *base)
{
    int hottest = INT32_MIN;
    for (int p = 0; p < GDDR7_MAX_MODULES; p++)
    {
        const volatile uint8_t *m = base + (size_t)p * GDDR7_MODULE_STRIDE;
        uint32_t vld = *(const volatile uint32_t *)(m + GDDR7_VALID_OFF);
        if (((vld >> 24) & 0xFu) != 0xFu)
            continue;
        uint32_t dq = *(const volatile uint32_t *)m;
        if ((dq & 0xFFFF0000u) == 0xBADF0000u)
            continue;
        int code = (int)((dq >> 16) & 0xFFu);
        if (code > 80) code = 80;
        int c = (code > 19) ? (code - 20) * 2 : -(40 - code * 2);
        if (c > -40 && c < 130 && c > hottest)
            hottest = c;
    }
    return hottest;
}

void gddr6_monitor_temperatures(void)
{
   while (1) {
        printf("\rVRAM Temps: |");
        for (uint32_t i = 0; i < ctx.num_devices; i++)
        {
            if (ctx.devices[i].mapped_addr == NULL || ctx.devices[i].mapped_addr == MAP_FAILED)
            {
                continue;
            }

            void *virt_addr = (uint8_t *) ctx.devices[i].mapped_addr + (ctx.devices[i].phys_addr  - ctx.devices[i].base_offset);
            if (ctx.devices[i].kind == VRAM_GDDR7)
            {
                int t = gddr7_hottest_module((const volatile uint8_t *)virt_addr);
                if (t == INT32_MIN)
                    printf("  n/a |");   /* keep the cell so columns stay aligned */
                else
                    printf(" %3d°C |", t);
                continue;
            }
            uint32_t read_result = *((uint32_t *)virt_addr);
            uint32_t temp = ((read_result & 0x00000fff) / 0x20);

            printf(" %3u°C |", temp);
        }
        fflush(stdout);
        sleep(1);
   }
}

void gddr6_cleanup(int signal)
{
    for (uint32_t i = 0; i < ctx.num_devices; i++)
    {
        if (ctx.devices[i].mapped_addr != NULL && ctx.devices[i].mapped_addr != MAP_FAILED)
        {
            munmap(ctx.devices[i].mapped_addr, ctx.devices[i].map_len ? ctx.devices[i].map_len : (size_t)PG_SZ);
            ctx.devices[i].mapped_addr = NULL;
        }
    }
    if (ctx.fd != -1)
    {
        close(ctx.fd);
        ctx.fd = -1;
    }
    if (ctx.devices)
    {
        free(ctx.devices);
        ctx.devices = NULL;
    }
    exit(signal);
}
