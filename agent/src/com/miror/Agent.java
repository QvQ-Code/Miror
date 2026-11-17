package com.miror;

import android.content.Context;

import java.io.OutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Shell-side capture agent. Encodes the device display as low-latency H.264
 * and writes framed access units to the process that connected over adb.
 */
public final class Agent {
    private static final int COLOR_FORMAT_SURFACE = 0x7F000789;
    private static final int CONFIGURE_ENCODE = 1;
    private static final int INFO_TRY_AGAIN = -1;
    private static final int INFO_FORMAT_CHANGED = -2;
    private static final int FLAG_KEY = 1;
    private static final int FLAG_CONFIG = 2;
    private static final int FLAG_PARTIAL = 8;
    private static final int KIND_CONFIG = 1;
    private static final int KIND_FRAME = 2;

    private final int maxLong;
    private final int maxShort;
    private final int capFps;
    private final int sizeTier;
    private final Object writeLock = new Object();
    private final DisplayMirror mirror = new DisplayMirror();
    private int activeLong;
    private int activeShort;
    private boolean light;

    private volatile boolean running = true;
    private volatile boolean accepted;
    private static volatile int exitCode;

    private int encoderMode;
    private Object codec;
    private Object surface;
    private Object server;
    private Object wakeLock;
    private OutputStream output;
    private int encW;
    private int encH;
    private int fps = 60;
    private int frames;
    private int keys;
    private long frameLogAt;
    private long sentAt;
    private long gapMaxNs;
    private long sendMaxNs;
    private long lateMaxNs;
    private int burstMax;
    private long windowBytes;
    private int windowFrames;
    private long paceAt;
    private long paceNs = 16_666_667L;
    private boolean preciseWait;
    private final ByteBuffer headerBuf = ByteBuffer.allocate(24).order(ByteOrder.LITTLE_ENDIAN);
    private byte[] heldData;
    private boolean heldKey;
    private long heldPts;
    private int repeats;
    private Unit pending;
    private boolean dropUntilKey;
    private int jumpMax;
    private Method dequeueOutput;
    private Method getOutputBuffer;
    private Method releaseOutputBuffer;
    private Object bufferInfo;
    private Field infoSize;
    private Field infoOffset;
    private Field infoPts;
    private Field infoFlags;
    private Metrics cachedMetrics;
    private long metricsAt;
    private volatile boolean metricsDirty = true;
    private AudioMirror audio;

    public Agent(int maxLong, int maxShort, int capFps, int sizeTier) {
        this.maxLong = maxLong;
        this.maxShort = maxShort;
        this.capFps = capFps == 30 ? 30 : 60;
        this.sizeTier = sizeTier == 2 ? 2 : sizeTier == 3 ? 3 : 1;
        this.activeLong = maxLong;
        this.activeShort = maxShort;
    }

    public static void main(String[] args) {
        int maxLong = 1920;
        int maxShort = 1080;
        int capFps = 60;
        int sizeTier = 1;
        if (args.length >= 1) {
            maxLong = Integer.parseInt(args[0]);
        }
        if (args.length >= 2) {
            maxShort = Integer.parseInt(args[1]);
        }
        if (args.length >= 3) {
            capFps = Integer.parseInt(args[2]);
        }
        if (args.length >= 4) {
            sizeTier = Integer.parseInt(args[3]);
        }
        final Agent agent = new Agent(maxLong, maxShort, capFps, sizeTier);
        try {
            prepareMainLooper();
            final Thread worker = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        Class.forName("android.os.Looper").getMethod("prepare").invoke(null);
                        agent.session();
                    } catch (Throwable ex) {
                        exitCode = 1;
                        System.out.println("MIROR_FAIL " + ex);
                        ex.printStackTrace(System.out);
                        System.out.flush();
                    } finally {
                        agent.cleanup();
                        quitMainLooper();
                    }
                }
            }, "miror");
            Class<?> handlerClass = Class.forName("android.os.Handler");
            Object handler = handlerClass.getConstructor().newInstance();
            handlerClass.getMethod("post", Runnable.class).invoke(handler, new Runnable() {
                @Override
                public void run() {
                    worker.start();
                }
            });
            Class.forName("android.os.Looper").getMethod("loop").invoke(null);
        } catch (Throwable ex) {
            exitCode = 1;
            System.out.println("MIROR_FAIL " + ex);
            ex.printStackTrace(System.out);
            System.out.flush();
        }
        System.exit(exitCode);
    }

    private void session() throws Exception {
        try {
            Class<?> process = Ref.cls("android.os.Process");
            Object pid = Ref.callStatic(process, "myPid", new Class<?>[0], new Object[0]);
            System.out.println("MIROR_PID " + pid);
            System.out.flush();
            Ref.callStatic(process, "setThreadPriority",
                    new Class<?>[] {int.class}, new Object[] {-8});
        } catch (Throwable ignored) {
        }
        relaxClock();

        int sdk = 0;
        try {
            sdk = Class.forName("android.os.Build$VERSION").getField("SDK_INT").getInt(null);
        } catch (Throwable ignored) {
        }
        Log.line("sdk=" + sdk);

        Object context = obtainContext();
        acquireWakeLock(context);
        watchDisplay(context);

        Class<?> serverClass = Ref.cls("android.net.LocalServerSocket");
        server = serverClass.getConstructor(String.class).newInstance("miror");
        Log.line("listening");
        System.out.println("MIROR_READY");
        System.out.flush();
        audio = new AudioMirror();
        audio.start(context);
        armAcceptWatchdog();

        Object socket = Ref.call(server, "accept", new Class<?>[0], new Object[0]);
        accepted = true;
        try {
            // 424 and 544 pictures fit in 64 KB. An 856 picture often does not,
            // so the write blocked and the fps number dipped.
            int buffer = sizeTier == 3 ? 1024 * 1024 : 64 * 1024;
            Ref.call(socket, "setSendBufferSize", new Class<?>[] {int.class}, new Object[] {buffer});
        } catch (Throwable ignored) {
        }
        output = (OutputStream) Ref.call(socket, "getOutputStream", new Class<?>[0], new Object[0]);

        long emptySince = System.nanoTime();
        while (running) {
            Metrics metrics = metricsNow();
            int[] fitted = sizeTier == 2
                    ? cleanHalf(metrics.width, metrics.height)
                    : fit(metrics.width, metrics.height, activeLong, activeShort);
            if (codec == null || fitted[0] != encW || fitted[1] != encH) {
                rebuild(context, fitted[0], fitted[1], metrics);
                emptySince = System.nanoTime();
            }
            if (!pump()) {
                if (frames == 0 && System.nanoTime() - emptySince > 6_000_000_000L) {
                    throw new IllegalStateException("encoder produced no frames");
                }
            }
        }
    }

    private void rebuild(Object context, int width, int height, Metrics metrics) throws Exception {
        heldData = null;
        pending = null;
        dropUntilKey = false;
        int bitrate = bitrateFor(width, height);
        int nextFps = metrics.fps;
        if (nextFps < 1) {
            nextFps = capFps;
        }
        if (nextFps > capFps) {
            nextFps = capFps;
        }
        if (light) {
            // 4 Mbps was a clear 30 fps picture at the tier-1 size. Scale by
            // pixels and by fps so every tier keeps that same bits per pixel.
            // Integer formula matches miror.scale.light_bitrate.
            int[] base = fit(metrics.width, metrics.height, 960, 540);
            long basePx = (long) base[0] * (long) base[1];
            long px = (long) width * (long) height;
            if (basePx < 1L) {
                basePx = px;
            }
            bitrate = (int) ((4_000_000L * nextFps * px) / (30L * basePx));
        }
        Object nextCodec = createEncoder(width, height, nextFps, bitrate);
        String codecName = String.valueOf(Ref.call(nextCodec, "getName", new Class<?>[0], new Object[0]));
        if (!light && codecName.toLowerCase().contains("unisoc")) {
            releaseQuiet(nextCodec, null, false);
            light = true;
            int[] smaller;
            if (sizeTier == 2) {
                smaller = cleanHalf(metrics.width, metrics.height);
            } else {
                activeLong = Math.min(maxLong, lightLong());
                activeShort = Math.min(maxShort, lightShort());
                smaller = fit(metrics.width, metrics.height, activeLong, activeShort);
            }
            activeLong = Math.max(smaller[0], smaller[1]);
            activeShort = Math.min(smaller[0], smaller[1]);
            Log.line("unisoc light " + smaller[0] + "x" + smaller[1]);
            rebuild(context, smaller[0], smaller[1], metrics);
            return;
        }
        Object nextSurface = Ref.call(nextCodec, "createInputSurface", new Class<?>[0], new Object[0]);
        try {
            Ref.call(nextSurface, "setFrameRate",
                    new Class<?>[] {float.class, int.class},
                    new Object[] {(float) nextFps, 1});
        } catch (Throwable ignored) {
        }
        Ref.call(nextCodec, "start", new Class<?>[0], new Object[0]);
        Log.line("encoder " + codecName + " " + width + "x" + height + " " + nextFps
                + "fps " + bitrate + " mode=" + encoderMode);
        try {
            if (codec == null) {
                mirror.open(context, nextSurface, metrics.width, metrics.height, width, height,
                        metrics.dpi, metrics.layerStack);
            } else {
                mirror.update(nextSurface, metrics.width, metrics.height, width, height,
                        metrics.dpi, metrics.layerStack);
            }
        } catch (Exception ex) {
            releaseQuiet(nextCodec, nextSurface);
            throw ex;
        }
        releaseQuiet(codec, surface);
        codec = nextCodec;
        surface = nextSurface;
        encW = width;
        encH = height;
        fps = nextFps;
        paceNs = 1_000_000_000L / nextFps;
        paceAt = 0L;
        requestSync(codec);
        System.out.println("MIROR_VIDEO " + width + " " + height + " " + nextFps);
        System.out.flush();
    }

    private void bindCodec() throws Exception {
        if (dequeueOutput != null) {
            return;
        }
        Class<?> infoClass = Ref.cls("android.media.MediaCodec$BufferInfo");
        Class<?> codecClass = Ref.cls("android.media.MediaCodec");
        dequeueOutput = Ref.method(codecClass, "dequeueOutputBuffer",
                new Class<?>[] {infoClass, long.class});
        getOutputBuffer = Ref.method(codecClass, "getOutputBuffer",
                new Class<?>[] {int.class});
        releaseOutputBuffer = Ref.method(codecClass, "releaseOutputBuffer",
                new Class<?>[] {int.class, boolean.class});
        infoSize = infoClass.getField("size");
        infoOffset = infoClass.getField("offset");
        infoPts = infoClass.getField("presentationTimeUs");
        infoFlags = infoClass.getField("flags");
        bufferInfo = infoClass.getConstructor().newInstance();
    }

    private boolean pump() throws Exception {
        bindCodec();
        if (dropUntilKey) {
            return catchUp();
        }
        // Do not take a picture out early. That frees an encoder buffer, the
        // next picture is made during the same slot, and the third one is
        // dropped until an intra. The drop is the flicker on the clean sizes.
        if (light && heldData != null && paceAt != 0L && System.nanoTime() < paceAt
                && (pending == null || sizeTier == 3)) {
            if (preciseWait || pending != null) {
                coast(paceAt);
            } else {
                long target = paceAt;
                while (System.nanoTime() < target) {
                }
                long late = System.nanoTime() - target;
                if (late > lateMaxNs) {
                    lateMaxNs = late;
                }
            }
        }
        long poll = (light && heldData != null) ? 0L : 8000L;
        Unit picture = pending;
        pending = null;
        if (picture == null) {
            picture = readUnit(poll);
        }
        if (picture == null) {
            if (light && heldData != null && paceAt != 0L && System.nanoTime() >= paceAt) {
                emitHeld();
                paceAt = System.nanoTime() + paceNs;
                return true;
            }
            return false;
        }
        picture = finishPicture(picture);
        Unit extra = null;
        if (light) {
            for (int n = 0; n < 40; n++) {
                Unit newer = readUnit(0L);
                if (newer == null) {
                    break;
                }
                newer = finishPicture(newer);
                if (extra == null && belongsTogether(picture, newer)) {
                    picture = join(picture, newer);
                    continue;
                }
                if (extra != null && belongsTogether(extra, newer)) {
                    extra = join(extra, newer);
                    continue;
                }
                if (extra == null) {
                    extra = newer;
                    continue;
                }
                // One extra picture waits for the next slot. Dropping it until
                // an intra breaks the reference and the clean picture flickers.
                pending = newer;
                break;
            }
        }
        int burst = 0;
        emitPicture(picture);
        burst++;
        if (extra != null) {
            emitPicture(extra);
            burst++;
        }
        if (burst > burstMax) {
            burstMax = burst;
        }
        if (light) {
            paceAt = System.nanoTime() + paceNs;
        }
        return true;
    }

    private boolean catchUp() throws Exception {
        Unit key = null;
        for (int n = 0; n < 40; n++) {
            Unit newer = readUnit(0L);
            if (newer == null) {
                break;
            }
            newer = finishPicture(newer);
            if (key != null && belongsTogether(key, newer)) {
                key = join(key, newer);
                continue;
            }
            if (!newer.partial && newer.key && newer.data != null && startsPicture(newer.data)) {
                key = newer;
                continue;
            }
            if (key != null) {
                pending = newer;
                break;
            }
        }
        if (key == null) {
            if (heldData != null && paceAt != 0L && System.nanoTime() >= paceAt) {
                emitHeld();
                paceAt = System.nanoTime() + paceNs;
                return true;
            }
            if (preciseWait) {
                try {
                    Thread.sleep(2L);
                } catch (InterruptedException ignored) {
                }
            } else {
                long target = System.nanoTime() + 2_000_000L;
                while (System.nanoTime() < target) {
                }
            }
            return false;
        }
        emitPicture(key);
        dropUntilKey = false;
        if (burstMax < 1) {
            burstMax = 1;
        }
        paceAt = System.nanoTime() + paceNs;
        return true;
    }

    private void emitPicture(Unit picture) throws java.io.IOException {
        emit(picture);
        if (picture.partial || picture.data == null || !startsPicture(picture.data)) {
            return;
        }
        heldData = picture.data;
        heldKey = picture.key;
        long step = paceNs / 1000L;
        if (step < 1L) {
            step = 1L;
        }
        if (picture.pts > heldPts) {
            heldPts = picture.pts;
        } else {
            heldPts += step;
        }
    }

    private void emitHeld() throws java.io.IOException {
        long step = paceNs / 1000L;
        if (step < 1L) {
            step = 1L;
        }
        heldPts += step;
        Unit again = new Unit();
        again.data = heldData;
        again.key = heldKey;
        again.pts = heldPts;
        repeats++;
        emit(again);
    }

    private Unit readUnit(long timeout) throws Exception {
        int index = (Integer) Ref.invoke(dequeueOutput, codec,
                new Object[] {bufferInfo, timeout});
        if (index == INFO_TRY_AGAIN) {
            return null;
        }
        if (index == INFO_FORMAT_CHANGED) {
            sendFormatConfig();
            return null;
        }
        if (index < 0) {
            return null;
        }
        int size = infoSize.getInt(bufferInfo);
        int offset = infoOffset.getInt(bufferInfo);
        long pts = infoPts.getLong(bufferInfo);
        int flags = infoFlags.getInt(bufferInfo);
        byte[] data = new byte[0];
        if (size > 0) {
            ByteBuffer buffer = (ByteBuffer) Ref.invoke(getOutputBuffer, codec, new Object[] {index});
            data = bytesRange(buffer, offset, size);
        }
        Ref.invoke(releaseOutputBuffer, codec, new Object[] {index, Boolean.FALSE});
        if (size <= 0) {
            return null;
        }
        Unit unit = new Unit();
        unit.data = normalize(data);
        unit.pts = pts;
        unit.key = (flags & FLAG_KEY) != 0;
        unit.config = (flags & FLAG_CONFIG) != 0;
        unit.partial = (flags & FLAG_PARTIAL) != 0;
        return unit;
    }

    private Unit finishPicture(Unit picture) throws Exception {
        int guard = 0;
        while (picture.partial && guard < 8) {
            guard++;
            Unit more = readUnit(3000L);
            if (more == null) {
                break;
            }
            picture = join(picture, more);
        }
        return picture;
    }

    private static boolean belongsTogether(Unit older, Unit newer) {
        return newer.partial || !startsPicture(newer.data);
    }

    private static Unit join(Unit older, Unit newer) {
        byte[] data = new byte[older.data.length + newer.data.length];
        System.arraycopy(older.data, 0, data, 0, older.data.length);
        System.arraycopy(newer.data, 0, data, older.data.length, newer.data.length);
        older.data = data;
        older.partial = newer.partial;
        older.key = older.key || newer.key;
        if (!newer.config) {
            older.config = false;
        }
        return older;
    }

    private static boolean startsPicture(byte[] data) {
        if (data == null) {
            return false;
        }
        int i = 0;
        while (i + 4 < data.length) {
            int mark = startMark(data, i);
            if (mark == 0) {
                i++;
                continue;
            }
            int nal = i + mark;
            if (nal >= data.length) {
                return false;
            }
            int type = data[nal] & 0x1F;
            if (type == 1 || type == 5) {
                return firstMb(data, nal + 1) == 0;
            }
            i = nal + 1;
        }
        return false;
    }

    private static int startMark(byte[] data, int i) {
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1) {
            return 3;
        }
        if (i + 3 < data.length && data[i] == 0 && data[i + 1] == 0
                && data[i + 2] == 0 && data[i + 3] == 1) {
            return 4;
        }
        return 0;
    }

    private static int firstMb(byte[] data, int offset) {
        int bit = offset * 8;
        int limit = data.length * 8;
        int zeros = 0;
        while (bit < limit && bitAt(data, bit) == 0) {
            zeros++;
            bit++;
            if (zeros > 16) {
                return -1;
            }
        }
        if (bit >= limit) {
            return -1;
        }
        bit++;
        int value = 1;
        for (int n = 0; n < zeros; n++) {
            if (bit >= limit) {
                return -1;
            }
            value = (value << 1) | bitAt(data, bit);
            bit++;
        }
        return value - 1;
    }

    private static int bitAt(byte[] data, int bit) {
        return (data[bit >> 3] >> (7 - (bit & 7))) & 1;
    }

    private void emit(Unit unit) throws java.io.IOException {
        int kind = unit.config ? KIND_CONFIG : KIND_FRAME;
        long started = System.nanoTime();
        send(kind, unit.key ? 1 : 0, unit.pts, encW, encH, unit.data);
        if (kind == KIND_FRAME) {
            long took = System.nanoTime() - started;
            if (took > sendMaxNs) {
                sendMaxNs = took;
            }
            windowBytes += unit.data.length;
            windowFrames++;
            if (unit.key) {
                keys++;
            }
            noteFrame();
        }
    }

    private static final class Unit {
        private byte[] data;
        private long pts;
        private boolean key;
        private boolean config;
        private boolean partial;
    }

    private void noteFrame() {
        long now = System.nanoTime();
        if (sentAt != 0L) {
            long gap = now - sentAt;
            if (gap > gapMaxNs) {
                gapMaxNs = gap;
            }
        }
        sentAt = now;
        frames++;
        if (frames == 1 || frames % 120 == 0) {
            long dt = frameLogAt == 0L ? 0L : (now - frameLogAt) / 1_000_000L;
            frameLogAt = now;
            long avg = windowFrames == 0 ? 0L : windowBytes / windowFrames;
            Log.line("frames=" + frames + " dt=" + dt + " max=" + (gapMaxNs / 1_000_000L)
                    + " send=" + (sendMaxNs / 1_000_000L) + " late=" + (lateMaxNs / 1_000_000L)
                    + " avg=" + avg + " keys=" + keys + " rep=" + repeats
                    + " burst=" + burstMax + " jump=" + jumpMax);
            gapMaxNs = 0L;
            sendMaxNs = 0L;
            lateMaxNs = 0L;
            burstMax = 0;
            jumpMax = 0;
            windowBytes = 0L;
            windowFrames = 0;
            keys = 0;
            repeats = 0;
        }
    }

    private void sendFormatConfig() {
        try {
            Object format = Ref.call(codec, "getOutputFormat", new Class<?>[0], new Object[0]);
            Object raw = Ref.call(format, "getByteBuffer",
                    new Class<?>[] {String.class}, new Object[] {"csd-0"});
            if (raw instanceof ByteBuffer) {
                ByteBuffer dup = ((ByteBuffer) raw).duplicate();
                byte[] data = new byte[dup.remaining()];
                dup.get(data);
                send(KIND_CONFIG, 1, 0L, encW, encH, normalize(data));
            }
        } catch (Throwable ex) {
            Log.line("csd " + ex);
        }
    }

    private void send(int kind, int flags, long pts, int width, int height, byte[] payload)
            throws java.io.IOException {
        if (output == null) {
            return;
        }
        if (payload == null) {
            payload = new byte[0];
        }
        headerBuf.clear();
        headerBuf.putInt(payload.length);
        headerBuf.put((byte) kind);
        headerBuf.put((byte) flags);
        headerBuf.putShort((short) 0);
        headerBuf.putLong(pts);
        headerBuf.putInt(width);
        headerBuf.putInt(height);
        synchronized (writeLock) {
            output.write(headerBuf.array());
            output.write(payload);
            output.flush();
        }
    }

    /**
     * Pin this thread's timer so a codec wait stays near the pace slot.
     * Without that pin, a sleep on this phone has returned tens of milliseconds
     * late, so the old path spun the whole slot instead.
     */
    private void relaxClock() {
        preciseWait = false;
        try {
            Class<?> os = Ref.cls("android.system.Os");
            Ref.callStatic(os, "prctl",
                    new Class<?>[] {int.class, long.class, long.class, long.class, long.class},
                    new Object[] {Integer.valueOf(29), Long.valueOf(1L), Long.valueOf(0L),
                            Long.valueOf(0L), Long.valueOf(0L)});
            preciseWait = true;
        } catch (Throwable ignored) {
        }
        if (preciseWait) {
            try {
                Class<?> process = Ref.cls("android.os.Process");
                Object tid = Ref.callStatic(process, "myTid", new Class<?>[0], new Object[0]);
                // Foreground group, not top-app. The mirrored app keeps the top slot.
                Ref.callStatic(process, "setThreadGroup",
                        new Class<?>[] {int.class, int.class},
                        new Object[] {tid, Integer.valueOf(1)});
            } catch (Throwable ignored) {
            }
        }
        Log.line(preciseWait ? "pace wait=slot" : "pace wait=spin");
    }

    private void coast(long target) {
        long left = target - System.nanoTime();
        if (preciseWait && left > 3_000_000L) {
            try {
                Thread.sleep((left - 2_000_000L) / 1_000_000L);
            } catch (InterruptedException ignored) {
            }
        }
        while (System.nanoTime() < target) {
        }
        long late = System.nanoTime() - target;
        if (late > lateMaxNs) {
            lateMaxNs = late;
        }
    }

    private Object createEncoder(int width, int height, int frameRate, int bitrate) throws Exception {
        Exception last = null;
        for (int mode = 0; mode < 3; mode++) {
            Object candidate = null;
            try {
                Class<?> codecClass = Ref.cls("android.media.MediaCodec");
                candidate = Ref.callStatic(codecClass, "createEncoderByType",
                        new Class<?>[] {String.class}, new Object[] {"video/avc"});
                Class<?> formatClass = Ref.cls("android.media.MediaFormat");
                Object format = Ref.callStatic(formatClass, "createVideoFormat",
                        new Class<?>[] {String.class, int.class, int.class},
                        new Object[] {"video/avc", width, height});
                applyFormat(format, bitrate, frameRate, mode);
                Class<?> surfaceClass = Ref.cls("android.view.Surface");
                Class<?> cryptoClass = Ref.cls("android.media.MediaCrypto");
                Ref.call(candidate, "configure",
                        new Class<?>[] {formatClass, surfaceClass, cryptoClass, int.class},
                        new Object[] {format, null, null, CONFIGURE_ENCODE});
                encoderMode = mode;
                return candidate;
            } catch (Exception ex) {
                last = ex;
                Log.line("encoder mode " + mode + " failed: " + ex);
                releaseQuiet(candidate, null);
            }
        }
        throw last == null ? new IllegalStateException("encoder failed") : last;
    }

    private void applyFormat(Object format, int bitrate, int frameRate, int mode) {
        setInt(format, "color-format", COLOR_FORMAT_SURFACE);
        setInt(format, "bitrate", bitrate);
        setInt(format, "frame-rate", frameRate);
        // One intra picture per second. Interval 0 re-encoded every frame, so
        // unchanged pixels shimmered. P-frames must be sent in order after this.
        setInt(format, "i-frame-interval", 1);
        if (light && mode == 0) {
            setInt(format, "max-fps-to-encoder", frameRate);
        }
        setLong(format, "repeat-previous-frame-after", 100000L);
        if (mode <= 1) {
            setInt(format, "bitrate-mode", 2);
            setInt(format, "priority", 0);
            setInt(format, "low-latency", 1);
            setInt(format, "max-bframes", 0);
            setInt(format, "operating-rate", frameRate);
            setInt(format, "prepend-sps-pps-to-idr-frames", 1);
            if (light && mode == 0) {
                setInt(format, "latency", 0);
            }
        }
        if (mode == 0) {
            // High profile keeps the same bitrate but blocks less than Baseline.
            setInt(format, "profile", 8);
            setInt(format, "level", 0x2000);
            // Full-range BT.709 matches the phone's screen. Mode 1 leaves
            // these out so a picky encoder can still start.
            setInt(format, "color-standard", 1);
            setInt(format, "color-range", 1);
            setInt(format, "color-transfer", 3);
        }
    }

    private static void requestSync(Object target) {
        try {
            Class<?> bundleClass = Ref.cls("android.os.Bundle");
            Object bundle = bundleClass.getConstructor().newInstance();
            Ref.call(bundle, "putInt", new Class<?>[] {String.class, int.class},
                    new Object[] {"request-sync", 0});
            Ref.call(target, "setParameters", new Class<?>[] {bundleClass}, new Object[] {bundle});
        } catch (Throwable ex) {
            Log.line("sync " + ex);
        }
    }

    private Metrics metricsNow() throws Exception {
        long now = System.nanoTime();
        if (cachedMetrics == null || metricsDirty || now - metricsAt >= 2_000_000_000L) {
            cachedMetrics = readMetrics();
            metricsAt = now;
            metricsDirty = false;
        }
        return cachedMetrics;
    }

    private Metrics readMetrics() throws Exception {
        Class<?> globalClass = Ref.cls("android.hardware.display.DisplayManagerGlobal");
        Object global = Ref.callStatic(globalClass, "getInstance", new Class<?>[0], new Object[0]);
        Object info = Ref.call(global, "getDisplayInfo", new Class<?>[] {int.class}, new Object[] {0});
        Metrics metrics = new Metrics();
        metrics.width = Ref.intField(info, "logicalWidth");
        metrics.height = Ref.intField(info, "logicalHeight");
        try {
            metrics.dpi = Ref.intField(info, "logicalDensityDpi");
        } catch (Throwable ignored) {
            metrics.dpi = 320;
        }
        try {
            metrics.layerStack = Ref.intField(info, "layerStack");
        } catch (Throwable ignored) {
            metrics.layerStack = 0;
        }
        metrics.fps = 60;
        try {
            float refresh = Ref.floatField(info, "refreshRate");
            if (refresh >= 20f && refresh < 60f) {
                metrics.fps = Math.round(refresh);
            }
        } catch (Throwable ignored) {
        }
        if (metrics.width <= 0 || metrics.height <= 0) {
            throw new IllegalStateException("display size unavailable");
        }
        return metrics;
    }

    private void watchDisplay(Object context) {
        try {
            final Object manager = Ref.call(context, "getSystemService",
                    new Class<?>[] {String.class}, new Object[] {"display"});
            Class<?> listenerClass = Ref.cls("android.hardware.display.DisplayManager$DisplayListener");
            Object listener = Proxy.newProxyInstance(listenerClass.getClassLoader(),
                    new Class<?>[] {listenerClass},
                    new java.lang.reflect.InvocationHandler() {
                        @Override
                        public Object invoke(Object proxy, java.lang.reflect.Method method, Object[] args) {
                            if ("onDisplayChanged".equals(method.getName())) {
                                metricsDirty = true;
                            }
                            return defaultValue(method.getReturnType());
                        }
                    });
            Class<?> handlerClass = Ref.cls("android.os.Handler");
            Object handler = handlerClass.getConstructor().newInstance();
            Ref.call(manager, "registerDisplayListener",
                    new Class<?>[] {listenerClass, handlerClass}, new Object[] {listener, handler});
        } catch (Throwable ex) {
            Log.line("display listener " + ex);
        }
    }

    private static Object obtainContext() throws Exception {
        Class<?> threadClass = Ref.cls("android.app.ActivityThread");
        Object thread = null;
        try {
            java.lang.reflect.Constructor<?> ctor = threadClass.getDeclaredConstructor();
            ctor.setAccessible(true);
            thread = ctor.newInstance();
            java.lang.reflect.Field current = threadClass.getDeclaredField("sCurrentActivityThread");
            current.setAccessible(true);
            if (current.get(null) == null) {
                current.set(null, thread);
            }
            try {
                java.lang.reflect.Field systemThread = threadClass.getDeclaredField("mSystemThread");
                systemThread.setAccessible(true);
                systemThread.setBoolean(thread, true);
            } catch (Throwable ignored) {
            }
        } catch (Throwable ex) {
            Log.line("activity thread ctor " + ex);
        }
        Object system = null;
        if (thread != null) {
            try {
                system = Ref.call(thread, "getSystemContext", new Class<?>[0], new Object[0]);
            } catch (Throwable ex) {
                Log.line("getSystemContext " + ex);
            }
        }
        if (system == null) {
            thread = Ref.callStatic(threadClass, "systemMain", new Class<?>[0], new Object[0]);
            system = Ref.call(thread, "getSystemContext", new Class<?>[0], new Object[0]);
        }
        if (system == null) {
            throw new IllegalStateException("system context missing");
        }
        try {
            Object shell = Ref.call(system, "createPackageContext",
                    new Class<?>[] {String.class, int.class},
                    new Object[] {"com.android.shell", 2});
            if (shell != null) {
                Object name = Ref.call(shell, "getPackageName", new Class<?>[0], new Object[0]);
                Log.line("package " + name);
                if ("com.android.shell".equals(name)) {
                    return shell;
                }
            }
        } catch (Throwable ex) {
            Log.line("createPackageContext " + ex);
        }
        Log.line("using shell wrapper");
        return new ShellContext((Context) system);
    }

    private void acquireWakeLock(Object context) {
        try {
            Object power = Ref.call(context, "getSystemService",
                    new Class<?>[] {String.class}, new Object[] {"power"});
            wakeLock = Ref.call(power, "newWakeLock",
                    new Class<?>[] {int.class, String.class},
                    new Object[] {1, "miror:mirror"});
            Ref.call(wakeLock, "setReferenceCounted",
                    new Class<?>[] {boolean.class}, new Object[] {Boolean.FALSE});
            Ref.call(wakeLock, "acquire", new Class<?>[0], new Object[0]);
            Log.line("wake lock");
        } catch (Throwable ex) {
            wakeLock = null;
            Log.line("wake lock " + ex);
        }
    }

    private void cleanup() {
        running = false;
        if (audio != null) {
            audio.stop();
            audio = null;
        }
        if (wakeLock != null) {
            try {
                Ref.call(wakeLock, "release", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            wakeLock = null;
        }
        try {
            mirror.close();
        } catch (Throwable ignored) {
        }
        releaseQuiet(codec, surface);
        codec = null;
        surface = null;
        if (server != null) {
            try {
                Ref.call(server, "close", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            server = null;
        }
    }

    private void armAcceptWatchdog() {
        Thread watchdog = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Thread.sleep(20000L);
                } catch (InterruptedException ignored) {
                    return;
                }
                if (!accepted) {
                    Log.line("no client");
                    System.exit(2);
                }
            }
        }, "miror-watch");
        watchdog.setDaemon(true);
        watchdog.start();
    }

    private static void releaseQuiet(Object targetCodec, Object targetSurface) {
        releaseQuiet(targetCodec, targetSurface, true);
    }

    private static void releaseQuiet(Object targetCodec, Object targetSurface, boolean started) {
        if (targetCodec != null) {
            if (started) {
                try {
                    Ref.call(targetCodec, "stop", new Class<?>[0], new Object[0]);
                } catch (Throwable ignored) {
                }
            }
            try {
                Ref.call(targetCodec, "release", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
        }
        if (targetSurface != null) {
            try {
                Ref.call(targetSurface, "release", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
        }
    }

    private static void prepareMainLooper() throws Exception {
        Class<?> looper = Class.forName("android.os.Looper");
        if (looper.getMethod("myLooper").invoke(null) == null) {
            looper.getMethod("prepareMainLooper").invoke(null);
        }
    }

    private static void quitMainLooper() {
        try {
            Object looper = Class.forName("android.os.Looper").getMethod("getMainLooper").invoke(null);
            try {
                looper.getClass().getMethod("quitSafely").invoke(looper);
            } catch (NoSuchMethodException ex) {
                looper.getClass().getMethod("quit").invoke(looper);
            }
        } catch (Throwable ignored) {
        }
    }

    static int[] cleanHalf(int srcW, int srcH) {
        // Nearest 8-pixel size to exactly half. A fractional scale like
        // 1080→464 is what makes the phone picture look warped.
        int width = (srcW / 2 + 4) & ~7;
        int height = (srcH / 2 + 4) & ~7;
        if (width < 16) {
            width = 16;
        }
        if (height < 16) {
            height = 16;
        }
        return new int[] {width, height};
    }

    private int lightLong() {
        if (sizeTier == 2) {
            return 1040;
        }
        if (sizeTier == 3) {
            return 1920;
        }
        return 960;
    }

    private int lightShort() {
        if (sizeTier == 1) {
            return 540;
        }
        return 1080;
    }

    static int[] fit(int srcW, int srcH, int maxLong, int maxShort) {
        int longSide = Math.max(srcW, srcH);
        int shortSide = Math.min(srcW, srcH);
        double scale = 1d;
        if (longSide > maxLong) {
            scale = Math.min(scale, (double) maxLong / (double) longSide);
        }
        if (shortSide > maxShort) {
            scale = Math.min(scale, (double) maxShort / (double) shortSide);
        }
        int width = align8(iround(srcW * scale));
        int height = align8(iround(srcH * scale));
        while (width >= 16 && height >= 16
                && (Math.max(width, height) > maxLong || Math.min(width, height) > maxShort)) {
            if (width >= height) {
                width -= 8;
            } else {
                height -= 8;
            }
        }
        if (width < 16) {
            width = 16;
        }
        if (height < 16) {
            height = 16;
        }
        return new int[] {width, height};
    }

    static int bitrateFor(int width, int height) {
        double ratio = (width * (double) height) / (1920d * 1080d);
        int bitrate = (int) Math.floor(12_000_000d * ratio + 0.5d);
        if (bitrate < 2_000_000) {
            bitrate = 2_000_000;
        }
        if (bitrate > 16_000_000) {
            bitrate = 16_000_000;
        }
        return bitrate;
    }

    private static int iround(double value) {
        return (int) Math.floor(value + 0.5d);
    }

    private static int align8(int value) {
        if (value < 16) {
            value = 16;
        }
        return value & ~7;
    }

    private static byte[] bytesRange(ByteBuffer buffer, int offset, int size) {
        ByteBuffer dup = buffer.duplicate();
        dup.position(offset);
        dup.limit(offset + size);
        byte[] out = new byte[size];
        dup.get(out);
        return out;
    }

    static byte[] normalize(byte[] data) {
        if (data == null || data.length == 0 || hasStartCode(data)) {
            return data;
        }
        if (data[0] == 1) {
            byte[] avcc = avccConfigToAnnexB(data);
            if (avcc != null) {
                return avcc;
            }
        }
        byte[] lengths = lengthsToAnnexB(data);
        return lengths == null ? data : lengths;
    }

    private static boolean hasStartCode(byte[] data) {
        if (data.length >= 4 && data[0] == 0 && data[1] == 0 && data[2] == 0 && data[3] == 1) {
            return true;
        }
        return data.length >= 3 && data[0] == 0 && data[1] == 0 && data[2] == 1;
    }

    private static byte[] avccConfigToAnnexB(byte[] data) {
        if (data.length < 7 || data[0] != 1) {
            return null;
        }
        int offset = 5;
        int spsCount = data[offset] & 0x1F;
        offset++;
        byte[][] parts = new byte[8][];
        int count = 0;
        try {
            for (int i = 0; i < spsCount; i++) {
                if (offset + 2 > data.length || count == parts.length) {
                    return null;
                }
                int length = ((data[offset] & 0xFF) << 8) | (data[offset + 1] & 0xFF);
                offset += 2;
                if (length <= 0 || offset + length > data.length) {
                    return null;
                }
                parts[count++] = annex(data, offset, length);
                offset += length;
            }
            if (offset >= data.length) {
                return join(parts, count);
            }
            int ppsCount = data[offset] & 0xFF;
            offset++;
            for (int i = 0; i < ppsCount; i++) {
                if (offset + 2 > data.length || count == parts.length) {
                    return null;
                }
                int length = ((data[offset] & 0xFF) << 8) | (data[offset + 1] & 0xFF);
                offset += 2;
                if (length <= 0 || offset + length > data.length) {
                    return null;
                }
                parts[count++] = annex(data, offset, length);
                offset += length;
            }
        } catch (RuntimeException ex) {
            return null;
        }
        return join(parts, count);
    }

    private static byte[] lengthsToAnnexB(byte[] data) {
        int offset = 0;
        byte[][] parts = new byte[64][];
        int count = 0;
        while (offset + 4 <= data.length) {
            int length = ((data[offset] & 0xFF) << 24)
                    | ((data[offset + 1] & 0xFF) << 16)
                    | ((data[offset + 2] & 0xFF) << 8)
                    | (data[offset + 3] & 0xFF);
            offset += 4;
            if (length <= 0 || offset + length > data.length || count == parts.length) {
                return null;
            }
            parts[count++] = annex(data, offset, length);
            offset += length;
        }
        if (offset != data.length || count == 0) {
            return null;
        }
        return join(parts, count);
    }

    private static byte[] annex(byte[] data, int offset, int length) {
        byte[] out = new byte[length + 4];
        out[3] = 1;
        System.arraycopy(data, offset, out, 4, length);
        return out;
    }

    private static byte[] join(byte[][] parts, int count) {
        int size = 0;
        for (int i = 0; i < count; i++) {
            size += parts[i].length;
        }
        byte[] out = new byte[size];
        int cursor = 0;
        for (int i = 0; i < count; i++) {
            System.arraycopy(parts[i], 0, out, cursor, parts[i].length);
            cursor += parts[i].length;
        }
        return out;
    }

    private static void setInt(Object format, String key, int value) {
        try {
            Ref.call(format, "setInteger",
                    new Class<?>[] {String.class, int.class}, new Object[] {key, value});
        } catch (Throwable ignored) {
        }
    }

    private static void setLong(Object format, String key, long value) {
        try {
            Ref.call(format, "setLong",
                    new Class<?>[] {String.class, long.class}, new Object[] {key, value});
        } catch (Throwable ignored) {
        }
    }

    private static Object defaultValue(Class<?> type) {
        if (type == boolean.class) {
            return Boolean.FALSE;
        }
        if (type == int.class) {
            return 0;
        }
        if (type == long.class) {
            return 0L;
        }
        return null;
    }

    private static final class Metrics {
        int width;
        int height;
        int dpi = 320;
        int layerStack;
        int fps = 60;
    }
}
