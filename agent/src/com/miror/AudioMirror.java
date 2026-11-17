package com.miror;

import android.content.Context;

import java.io.OutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Playback audio on its own socket. A failure here must not touch the video encoder.
 */
final class AudioMirror {
    private static final int SAMPLE_RATE = 48000;
    private static final int CHANNELS = 2;
    private static final int CHANNEL_IN_STEREO = 0xC;
    private static final int ENCODING_PCM_16BIT = 2;
    private static final int SOURCE_REMOTE_SUBMIX = 8;
    private static final int RECORDING = 3;
    private static final int USAGE_MATCH = 1;
    private static final int ROUTE_LOOP_BACK = 2;
    private static final int AAC_LC = 2;
    private static final int CONFIGURE_ENCODE = 1;
    private static final int INFO_TRY_AGAIN = -1;
    private static final int INFO_FORMAT_CHANGED = -2;
    private static final int KIND_CONFIG = 1;
    private static final int KIND_FRAME = 2;
    private static final int FLAG_CONFIG = 2;
    private static final int PCM_BYTES = 3840;

    private final Object lock = new Object();
    private volatile boolean running;
    private Thread thread;
    private Object server;
    private Object record;
    private Object codec;
    private Object policy;
    private Object audioManager;
    private OutputStream output;
    private boolean configSent;
    private final ByteBuffer headerBuf = ByteBuffer.allocate(24).order(ByteOrder.LITTLE_ENDIAN);

    private Method dequeueInput;
    private Method getInputBuffer;
    private Method queueInput;
    private Method dequeueOutput;
    private Method getOutputBuffer;
    private Method releaseOutputBuffer;
    private Object bufferInfo;
    private Field infoSize;
    private Field infoOffset;
    private Field infoPts;
    private Field infoFlags;

    void start(final Object context) {
        running = true;
        thread = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    capture(context);
                } catch (Throwable ex) {
                    Log.line("audio " + ex);
                } finally {
                    release();
                }
            }
        }, "miror-audio");
        thread.setDaemon(true);
        thread.start();
    }

    void stop() {
        running = false;
        Object recording = record;
        if (recording != null) {
            try {
                Ref.call(recording, "stop", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
        }
        closeServer();
        Thread current = thread;
        if (current != null && current != Thread.currentThread()) {
            try {
                current.join(400L);
            } catch (InterruptedException ignored) {
            }
        }
    }

    private void capture(Object context) throws Exception {
        // Listen first. The recorder and encoder stay closed until a PC
        // connects, and they close again when that PC drops the socket.
        // The picture thread never shares this work.
        Class<?> serverClass = Ref.cls("android.net.LocalServerSocket");
        server = serverClass.getConstructor(String.class).newInstance("miror-audio");
        Log.line("audio siap");
        System.out.println("MIROR_AUDIO_READY");
        System.out.flush();
        while (running) {
            Object socket;
            try {
                socket = Ref.call(server, "accept", new Class<?>[0], new Object[0]);
            } catch (Throwable ex) {
                if (running) {
                    Log.line("audio " + ex);
                }
                return;
            }
            if (!running || socket == null) {
                return;
            }
            boolean again = true;
            try {
                again = serve(context, socket);
            } catch (java.io.IOException closed) {
                again = running;
            } catch (Throwable ex) {
                if (running) {
                    Log.line("audio " + ex);
                }
                again = false;
            } finally {
                releaseCapture();
                try {
                    Ref.call(socket, "close", new Class<?>[0], new Object[0]);
                } catch (Throwable ignored) {
                }
            }
            if (!again || !running) {
                return;
            }
            Log.line("audio jeda");
        }
    }

    private boolean serve(Object context, Object socket) throws Exception {
        try {
            Ref.call(socket, "setSendBufferSize", new Class<?>[] {int.class}, new Object[] {64 * 1024});
        } catch (Throwable ignored) {
        }
        output = (OutputStream) Ref.call(socket, "getOutputStream", new Class<?>[0], new Object[0]);
        record = openRecord(context);
        if (record == null) {
            Log.line("audio ditolak");
            return false;
        }
        codec = openEncoder();
        bindCodec();
        configSent = false;
        byte[] pcm = new byte[PCM_BYTES];
        long pts = 0L;
        while (running) {
            Object read = Ref.call(record, "read",
                    new Class<?>[] {byte[].class, int.class, int.class},
                    new Object[] {pcm, 0, pcm.length});
            int size = read instanceof Number ? ((Number) read).intValue() : -1;
            if (size < 0 || !running) {
                break;
            }
            if (size < 4) {
                continue;
            }
            size -= size % 4;
            int queued = queuePcm(pcm, size, pts);
            if (queued > 0) {
                pts += (queued / 4L) * 1_000_000L / SAMPLE_RATE;
            }
            drain();
        }
        return running;
    }

    private Object openRecord(Object context) {
        try {
            return openPolicy(context);
        } catch (Throwable ex) {
            Log.line("audio policy " + ex);
        }
        try {
            return openSubmix();
        } catch (Throwable ex) {
            Log.line("audio submix " + ex);
        }
        return null;
    }

    private Object openPolicy(Object context) throws Exception {
        Class<?> attributes = Ref.cls("android.media.AudioAttributes");
        Class<?> ruleClass = Ref.cls("android.media.audiopolicy.AudioMixingRule");
        Class<?> ruleBuilder = Ref.cls("android.media.audiopolicy.AudioMixingRule$Builder");
        Object rules = ruleBuilder.getConstructor().newInstance();
        int[] usages = new int[] {1, 14, 0, 13};
        for (int usage : usages) {
            rules = Ref.call(rules, "addRule",
                    new Class<?>[] {attributes, int.class},
                    new Object[] {attributesFor(usage), Integer.valueOf(USAGE_MATCH)});
        }
        Object rule = Ref.call(rules, "build", new Class<?>[0], new Object[0]);
        Class<?> mixClass = Ref.cls("android.media.audiopolicy.AudioMix");
        Class<?> mixBuilder = Ref.cls("android.media.audiopolicy.AudioMix$Builder");
        Object mixing = mixBuilder.getConstructor(ruleClass).newInstance(rule);
        Class<?> formatClass = Ref.cls("android.media.AudioFormat");
        mixing = Ref.call(mixing, "setFormat",
                new Class<?>[] {formatClass}, new Object[] {pcmFormat()});
        mixing = Ref.call(mixing, "setRouteFlags",
                new Class<?>[] {int.class}, new Object[] {Integer.valueOf(ROUTE_LOOP_BACK)});
        Object mix = Ref.call(mixing, "build", new Class<?>[0], new Object[0]);
        Class<?> policyClass = Ref.cls("android.media.audiopolicy.AudioPolicy");
        Class<?> policyBuilder = Ref.cls("android.media.audiopolicy.AudioPolicy$Builder");
        Object built = policyBuilder.getConstructor(Context.class).newInstance(context);
        built = Ref.call(built, "addMix", new Class<?>[] {mixClass}, new Object[] {mix});
        policy = Ref.call(built, "build", new Class<?>[0], new Object[0]);
        audioManager = Ref.call(context, "getSystemService",
                new Class<?>[] {String.class}, new Object[] {"audio"});
        Object status = Ref.call(audioManager, "registerAudioPolicy",
                new Class<?>[] {policyClass}, new Object[] {policy});
        int code = status instanceof Number ? ((Number) status).intValue() : -1;
        if (code != 0) {
            throw new IllegalStateException("register " + code);
        }
        Object sink = Ref.call(policy, "createAudioRecordSink",
                new Class<?>[] {mixClass}, new Object[] {mix});
        if (sink == null) {
            throw new IllegalStateException("sink kosong");
        }
        startOrThrow(sink);
        return sink;
    }

    private Object openSubmix() throws Exception {
        Class<?> recordClass = Ref.cls("android.media.AudioRecord");
        Object minRaw = Ref.callStatic(recordClass, "getMinBufferSize",
                new Class<?>[] {int.class, int.class, int.class},
                new Object[] {SAMPLE_RATE, CHANNEL_IN_STEREO, ENCODING_PCM_16BIT});
        int min = minRaw instanceof Number ? ((Number) minRaw).intValue() : -1;
        if (min < PCM_BYTES) {
            min = PCM_BYTES;
        }
        Object sink = recordClass
                .getConstructor(int.class, int.class, int.class, int.class, int.class)
                .newInstance(SOURCE_REMOTE_SUBMIX, SAMPLE_RATE, CHANNEL_IN_STEREO,
                        ENCODING_PCM_16BIT, min * 2);
        startOrThrow(sink);
        return sink;
    }

    private void startOrThrow(Object sink) throws Exception {
        Ref.call(sink, "startRecording", new Class<?>[0], new Object[0]);
        Object state = Ref.call(sink, "getRecordingState", new Class<?>[0], new Object[0]);
        int code = state instanceof Number ? ((Number) state).intValue() : 0;
        if (code != RECORDING) {
            throw new IllegalStateException("recording " + code);
        }
    }

    private Object attributesFor(int usage) throws Exception {
        Class<?> builderClass = Ref.cls("android.media.AudioAttributes$Builder");
        Object builder = builderClass.getConstructor().newInstance();
        builder = Ref.call(builder, "setUsage", new Class<?>[] {int.class}, new Object[] {usage});
        return Ref.call(builder, "build", new Class<?>[0], new Object[0]);
    }

    private Object pcmFormat() throws Exception {
        Class<?> builderClass = Ref.cls("android.media.AudioFormat$Builder");
        Object builder = builderClass.getConstructor().newInstance();
        builder = Ref.call(builder, "setSampleRate",
                new Class<?>[] {int.class}, new Object[] {SAMPLE_RATE});
        builder = Ref.call(builder, "setEncoding",
                new Class<?>[] {int.class}, new Object[] {ENCODING_PCM_16BIT});
        builder = Ref.call(builder, "setChannelMask",
                new Class<?>[] {int.class}, new Object[] {CHANNEL_IN_STEREO});
        return Ref.call(builder, "build", new Class<?>[0], new Object[0]);
    }

    private Object openEncoder() throws Exception {
        Class<?> codecClass = Ref.cls("android.media.MediaCodec");
        Object encoder = Ref.callStatic(codecClass, "createEncoderByType",
                new Class<?>[] {String.class}, new Object[] {"audio/mp4a-latm"});
        Class<?> formatClass = Ref.cls("android.media.MediaFormat");
        Object format = Ref.callStatic(formatClass, "createAudioFormat",
                new Class<?>[] {String.class, int.class, int.class},
                new Object[] {"audio/mp4a-latm", SAMPLE_RATE, CHANNELS});
        Ref.call(format, "setInteger",
                new Class<?>[] {String.class, int.class}, new Object[] {"bitrate", 128000});
        Ref.call(format, "setInteger",
                new Class<?>[] {String.class, int.class}, new Object[] {"aac-profile", AAC_LC});
        Ref.call(format, "setInteger",
                new Class<?>[] {String.class, int.class}, new Object[] {"max-input-size", 8192});
        Class<?> surfaceClass = Ref.cls("android.view.Surface");
        Class<?> cryptoClass = Ref.cls("android.media.MediaCrypto");
        Ref.call(encoder, "configure",
                new Class<?>[] {formatClass, surfaceClass, cryptoClass, int.class},
                new Object[] {format, null, null, CONFIGURE_ENCODE});
        Ref.call(encoder, "start", new Class<?>[0], new Object[0]);
        return encoder;
    }

    private void bindCodec() throws Exception {
        Class<?> infoClass = Ref.cls("android.media.MediaCodec$BufferInfo");
        Class<?> codecClass = Ref.cls("android.media.MediaCodec");
        dequeueInput = Ref.method(codecClass, "dequeueInputBuffer", new Class<?>[] {long.class});
        getInputBuffer = Ref.method(codecClass, "getInputBuffer", new Class<?>[] {int.class});
        queueInput = Ref.method(codecClass, "queueInputBuffer",
                new Class<?>[] {int.class, int.class, int.class, long.class, int.class});
        dequeueOutput = Ref.method(codecClass, "dequeueOutputBuffer",
                new Class<?>[] {infoClass, long.class});
        getOutputBuffer = Ref.method(codecClass, "getOutputBuffer", new Class<?>[] {int.class});
        releaseOutputBuffer = Ref.method(codecClass, "releaseOutputBuffer",
                new Class<?>[] {int.class, boolean.class});
        infoSize = infoClass.getField("size");
        infoOffset = infoClass.getField("offset");
        infoPts = infoClass.getField("presentationTimeUs");
        infoFlags = infoClass.getField("flags");
        bufferInfo = infoClass.getConstructor().newInstance();
    }

    private int queuePcm(byte[] pcm, int size, long pts) throws Exception {
        Object indexRaw = Ref.invoke(dequeueInput, codec, new Object[] {Long.valueOf(0L)});
        int index = indexRaw instanceof Number ? ((Number) indexRaw).intValue() : -1;
        if (index < 0) {
            return 0;
        }
        int queued = 0;
        Object raw = Ref.invoke(getInputBuffer, codec, new Object[] {Integer.valueOf(index)});
        if (raw instanceof ByteBuffer) {
            ByteBuffer input = (ByteBuffer) raw;
            input.clear();
            int room = input.remaining();
            queued = size < room ? size : room;
            queued -= queued % 4;
            if (queued >= 4) {
                input.put(pcm, 0, queued);
            } else {
                queued = 0;
            }
        }
        Ref.invoke(queueInput, codec, new Object[] {
                Integer.valueOf(index), Integer.valueOf(0), Integer.valueOf(queued),
                Long.valueOf(pts), Integer.valueOf(0)});
        return queued;
    }

    private void drain() throws Exception {
        while (running) {
            Object indexRaw = Ref.invoke(dequeueOutput, codec, new Object[] {bufferInfo, Long.valueOf(0L)});
            int index = indexRaw instanceof Number ? ((Number) indexRaw).intValue() : INFO_TRY_AGAIN;
            if (index == INFO_TRY_AGAIN) {
                return;
            }
            if (index == INFO_FORMAT_CHANGED) {
                sendConfig();
                continue;
            }
            if (index < 0) {
                return;
            }
            int size = infoSize.getInt(bufferInfo);
            int offset = infoOffset.getInt(bufferInfo);
            long pts = infoPts.getLong(bufferInfo);
            int flags = infoFlags.getInt(bufferInfo);
            Object raw = Ref.invoke(getOutputBuffer, codec, new Object[] {Integer.valueOf(index)});
            byte[] data = new byte[Math.max(0, size)];
            if (raw instanceof ByteBuffer && size > 0) {
                ByteBuffer encoded = ((ByteBuffer) raw).duplicate();
                encoded.position(offset);
                encoded.limit(offset + size);
                encoded.get(data);
            }
            Ref.invoke(releaseOutputBuffer, codec, new Object[] {Integer.valueOf(index), Boolean.FALSE});
            if ((flags & FLAG_CONFIG) != 0) {
                send(KIND_CONFIG, data, pts);
                configSent = true;
                continue;
            }
            if (!configSent) {
                sendConfig();
            }
            if (data.length > 0) {
                send(KIND_FRAME, data, pts);
            }
        }
    }

    private void sendConfig() {
        if (configSent || codec == null) {
            return;
        }
        try {
            Object format = Ref.call(codec, "getOutputFormat", new Class<?>[0], new Object[0]);
            Object raw = Ref.call(format, "getByteBuffer",
                    new Class<?>[] {String.class}, new Object[] {"csd-0"});
            if (raw instanceof ByteBuffer) {
                ByteBuffer dup = ((ByteBuffer) raw).duplicate();
                byte[] data = new byte[dup.remaining()];
                dup.get(data);
                if (data.length > 0) {
                    send(KIND_CONFIG, data, 0L);
                    configSent = true;
                }
            }
        } catch (Throwable ex) {
            Log.line("audio csd " + ex);
        }
    }

    private void send(int kind, byte[] payload, long pts) throws java.io.IOException {
        OutputStream stream = output;
        if (stream == null || payload == null) {
            return;
        }
        headerBuf.clear();
        headerBuf.putInt(payload.length);
        headerBuf.put((byte) kind);
        headerBuf.put((byte) 0);
        headerBuf.putShort((short) 0);
        headerBuf.putLong(pts);
        headerBuf.putInt(SAMPLE_RATE);
        headerBuf.putInt(CHANNELS);
        synchronized (lock) {
            stream.write(headerBuf.array());
            stream.write(payload);
            stream.flush();
        }
    }

    private void closeServer() {
        Object current = server;
        if (current == null) {
            return;
        }
        try {
            Ref.call(current, "close", new Class<?>[0], new Object[0]);
        } catch (Throwable ignored) {
        }
    }

    private void release() {
        releaseCapture();
        closeServer();
        server = null;
    }

    private void releaseCapture() {
        output = null;
        configSent = false;
        if (record != null) {
            try {
                Ref.call(record, "stop", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            try {
                Ref.call(record, "release", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            record = null;
        }
        if (codec != null) {
            try {
                Ref.call(codec, "stop", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            try {
                Ref.call(codec, "release", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            codec = null;
        }
        if (policy != null && audioManager != null) {
            try {
                Ref.call(audioManager, "unregisterAudioPolicy",
                        new Class<?>[] {policy.getClass()}, new Object[] {policy});
            } catch (Throwable ignored) {
            }
            policy = null;
            audioManager = null;
        }
    }
}
