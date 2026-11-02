#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/videodev2.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* No values returned by ioctl/clock_gettime are changed. This Linux audit
 * observes the driver's thread-local DQBUF -> sync GET -> REAL -> MONO path. */
#define DQBUF_REQUEST 0xc0585611UL
#define SYNC_GET_INFO 0xc0285302UL
#define GAP_LIMIT_NS INT64_C(1000000)
static int (*next_clock)(clockid_t, struct timespec *);
static int (*next_ioctl)(int, unsigned long, ...);
static int log_fd = -1;
static uint64_t max_events = 4096, max_bytes = 8U*1024U*1024U, every = 60;
static uint64_t events, dropped;
static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;

typedef struct {
    int busy, fd, have_dq, have_real, camera_id;
    int have_previous_raw, have_previous_converted, raw_non_increasing;
    uint32_t sequence, buffer_index, flags, sync_hz;
    uint64_t sync_counter, frames;
    int64_t source_ns, previous_raw_ns, raw_delta_ns;
    int64_t realtime_ns, real_before_mono_ns, real_after_mono_ns;
    int64_t previous_converted_ns, previous_offset_ns;
} thread_state;
static __thread thread_state t = {.fd=-1,.camera_id=-1};

static int raw_clock(clockid_t id, struct timespec *ts) {
    return next_clock ? next_clock(id,ts) : (int)syscall(SYS_clock_gettime,id,ts);
}
static int64_t ns(const struct timespec *ts) {
    return (int64_t)ts->tv_sec*INT64_C(1000000000)+(int64_t)ts->tv_nsec;
}
static uint64_t bounded_env(const char *name,uint64_t fallback,uint64_t min,uint64_t max) {
    const char *s=getenv(name);char *end=NULL;
    if (!s || !*s) return fallback;
    errno=0;unsigned long long v=strtoull(s,&end,10);
    return !errno && end && !*end && v>=min && v<=max ? (uint64_t)v : fallback;
}
static void emit(const char *line,size_t length) {
    if (log_fd<0 || length==0) return;
    if (pthread_mutex_trylock(&log_lock)!=0) {
        __atomic_add_fetch(&dropped,1,__ATOMIC_RELAXED);return;
    }
    struct stat info;
    if (events<max_events && fstat(log_fd,&info)==0 && S_ISREG(info.st_mode)
        && info.st_size>=0 && (uint64_t)info.st_size+length<=max_bytes) {
        (void)syscall(SYS_write,log_fd,line,length);events++;
    }
    pthread_mutex_unlock(&log_lock);
}
__attribute__((constructor)) static void audit_init(void) {
    int saved=errno;t.busy++;
    *(void **)(&next_clock)=dlsym(RTLD_NEXT,"clock_gettime");
    *(void **)(&next_ioctl)=dlsym(RTLD_NEXT,"ioctl");
    const char *path=getenv("CLOCK_AUDIT_LOG");
    every=bounded_env("CLOCK_AUDIT_EVERY",60,1,1000000);
    max_events=bounded_env("CLOCK_AUDIT_MAX_EVENTS",4096,10,100000);
    max_bytes=bounded_env("CLOCK_AUDIT_MAX_BYTES",8U*1024U*1024U,65536,64U*1024U*1024U);
    if (path && *path) {
        log_fd=open(path,O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC|O_NOFOLLOW,0600);
        struct stat info;
        if (log_fd>=0 && (fstat(log_fd,&info)!=0 || !S_ISREG(info.st_mode))) {close(log_fd);log_fd=-1;}
    }
    char line[512];int length=snprintf(line,sizeof(line),
        "{\"event\":\"init\",\"pid\":%ld,\"read_only_values\":true,\"gap_threshold_ns\":%" PRId64
        ",\"periodic_every\":%" PRIu64 ",\"max_events\":%" PRIu64 ",\"max_bytes\":%" PRIu64 "}\n",
        (long)getpid(),GAP_LIMIT_NS,every,max_events,max_bytes);
    if (length>0 && (size_t)length<sizeof(line)) emit(line,(size_t)length);
    t.busy--;errno=saved;
}

int ioctl(int fd,unsigned long request,...) {
    va_list ap;va_start(ap,request);void *arg=va_arg(ap,void *);va_end(ap);
    int result=next_ioctl ? next_ioctl(fd,request,arg) : (int)syscall(SYS_ioctl,fd,request,arg);
    int saved=errno;
    /* The driver's xioctl(int request) sign-extends 0xc0585611 on AArch64.
     * Match Linux's 32-bit request code but forward the original word above. */
    uint32_t request32=(uint32_t)request;
    if (log_fd>=0 && !t.busy && result==0 && arg) {
        t.busy++;
        if (request32==(uint32_t)DQBUF_REQUEST) {
            const struct v4l2_buffer *b=(const struct v4l2_buffer *)arg;
            if (t.fd!=fd) {
                t.have_previous_raw=t.have_previous_converted=0;t.camera_id=-1;
                t.frames=0;t.sync_hz=0;t.sync_counter=0;
            }
            t.fd=fd;t.have_dq=1;t.have_real=0;
            t.sequence=b->sequence;t.buffer_index=b->index;t.flags=b->flags;
            t.source_ns=(int64_t)b->timestamp.tv_sec*INT64_C(1000000000)
                        +(int64_t)b->timestamp.tv_usec*INT64_C(1000);
            t.raw_delta_ns=t.have_previous_raw?t.source_ns-t.previous_raw_ns:0;
            t.raw_non_increasing=t.have_previous_raw && t.source_ns<=t.previous_raw_ns;
            t.previous_raw_ns=t.source_ns;t.have_previous_raw=1;
        } else if (request32==(uint32_t)SYNC_GET_INFO && t.have_dq) {
            const unsigned char *p=(const unsigned char *)arg;
            t.camera_id=p[0];memcpy(&t.sync_counter,p+8,sizeof(t.sync_counter));
            memcpy(&t.sync_hz,p+32,sizeof(t.sync_hz));
        }
        t.busy--;
    }
    errno=saved;return result;
}

int clock_gettime(clockid_t id,struct timespec *ts) {
    if (log_fd<0 || t.busy || !t.have_dq) return raw_clock(id,ts);
    if (id==CLOCK_REALTIME) {
        int incoming_errno=errno;struct timespec before,after;t.busy++;
        int before_ok=raw_clock(CLOCK_MONOTONIC,&before);
        errno=incoming_errno;
        int result=raw_clock(id,ts);int saved=errno;
        int after_ok=raw_clock(CLOCK_MONOTONIC,&after);
        if (result==0 && before_ok==0 && after_ok==0) {
            t.realtime_ns=ns(ts);t.real_before_mono_ns=ns(&before);
            t.real_after_mono_ns=ns(&after);t.have_real=1;
        } else t.have_real=0;
        t.busy--;errno=saved;return result;
    }
    int result=raw_clock(id,ts);int saved=errno;
    if (id==CLOCK_MONOTONIC && result==0 && t.have_real) {
        t.busy++;
        int64_t mono_ns=ns(ts),offset_ns=t.realtime_ns-mono_ns;
        int64_t converted_ns=t.source_ns+offset_ns;
        int64_t converted_delta=t.have_previous_converted?converted_ns-t.previous_converted_ns:0;
        int converted_non_increasing=t.have_previous_converted && converted_ns<=t.previous_converted_ns;
        int64_t gap_lower=mono_ns-t.real_after_mono_ns;
        int64_t gap_upper=mono_ns-t.real_before_mono_ns;
        int64_t bracket=t.real_after_mono_ns-t.real_before_mono_ns;
        t.frames++;
        int anomalous=t.raw_non_increasing || converted_non_increasing
                      || gap_lower>GAP_LIMIT_NS || bracket>GAP_LIMIT_NS;
        if (anomalous || t.frames==1 || t.frames%every==0) {
            char line[2048];int length=snprintf(line,sizeof(line),
                "{\"event\":\"clock_pair\",\"pid\":%ld,\"tid\":%ld,\"camera_id\":%d,\"fd\":%d,"
                "\"frame\":%" PRIu64 ",\"v4l2_sequence\":%" PRIu32 ",\"v4l2_index\":%" PRIu32 ","
                "\"v4l2_flags\":%" PRIu32 ",\"v4l2_timestamp_mask\":%u,\"sync_hz\":%" PRIu32 ",\"sync_counter\":%" PRIu64 ","
                "\"source_ns\":%" PRId64 ",\"source_delta_ns\":%" PRId64 ","
                "\"realtime_ns\":%" PRId64 ",\"paired_monotonic_ns\":%" PRId64 ","
                "\"real_before_monotonic_ns\":%" PRId64 ",\"real_after_monotonic_ns\":%" PRId64 ","
                "\"real_bracket_ns\":%" PRId64 ",\"pair_gap_lower_ns\":%" PRId64 ",\"pair_gap_upper_ns\":%" PRId64 ","
                "\"offset_ns\":%" PRId64 ",\"offset_delta_ns\":%" PRId64 ","
                "\"converted_ns\":%" PRId64 ",\"converted_ros_us_ns\":%" PRId64 ",\"converted_delta_ns\":%" PRId64 ","
                "\"raw_non_increasing\":%s,\"converted_non_increasing\":%s,\"anomaly\":%s,\"log_contention_dropped\":%" PRIu64 "}\n",
                (long)getpid(),(long)syscall(SYS_gettid),t.camera_id,t.fd,t.frames,t.sequence,t.buffer_index,t.flags,
                (unsigned)(t.flags&V4L2_BUF_FLAG_TIMESTAMP_MASK),t.sync_hz,t.sync_counter,
                t.source_ns,t.raw_delta_ns,t.realtime_ns,mono_ns,t.real_before_mono_ns,t.real_after_mono_ns,
                bracket,gap_lower,gap_upper,offset_ns,t.have_previous_converted?offset_ns-t.previous_offset_ns:0,
                converted_ns,(converted_ns/1000)*1000,converted_delta,
                t.raw_non_increasing?"true":"false",converted_non_increasing?"true":"false",anomalous?"true":"false",
                __atomic_load_n(&dropped,__ATOMIC_RELAXED));
            if (length>0 && (size_t)length<sizeof(line)) emit(line,(size_t)length);
        }
        t.previous_converted_ns=converted_ns;t.previous_offset_ns=offset_ns;
        t.have_previous_converted=1;t.have_real=0;t.have_dq=0;
        t.busy--;
    }
    errno=saved;return result;
}
