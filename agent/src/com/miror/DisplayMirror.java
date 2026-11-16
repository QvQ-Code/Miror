package com.miror;

import java.lang.reflect.Method;

/**
 * Mirrors the primary display onto an encoder surface.
 * Tries the shell display API first, then the composer display token.
 */
final class DisplayMirror {
    private static final int FLAG_PUBLIC = 1;
    private static final int FLAG_AUTO_MIRROR = 16;

    private Object virtualDisplay;
    private Object displayToken;
    private Object surfaceControl;
    private Class<?> serverDisplayControl;
    private String path = "";

    String path() {
        return path;
    }

    void open(Object context, Object surface, int deviceW, int deviceH, int encW, int encH, int dpi,
            int layerStack) throws Exception {
        Exception last = null;
        try {
            openDisplayManager(context, surface, encW, encH, dpi);
            path = "display-manager";
            Log.line("mirror path=display-manager");
            return;
        } catch (Throwable ex) {
            last = asException(ex);
            Log.line("display-manager failed: " + ex);
            close();
        }
        try {
            openSurfaceControl(surface, deviceW, deviceH, encW, encH, layerStack, false);
            path = "surface-control";
            Log.line("mirror path=surface-control");
            return;
        } catch (Throwable ex) {
            last = asException(ex);
            Log.line("surface-control failed: " + ex);
            close();
        }
        try {
            openSurfaceControl(surface, deviceW, deviceH, encW, encH, layerStack, true);
            path = "display-control";
            Log.line("mirror path=display-control");
            return;
        } catch (Throwable ex) {
            last = asException(ex);
            Log.line("display-control failed: " + ex);
            close();
        }
        if (last != null) {
            throw last;
        }
        throw new IllegalStateException("no mirror path");
    }

    void update(Object surface, int deviceW, int deviceH, int encW, int encH, int dpi, int layerStack)
            throws Exception {
        if (virtualDisplay != null) {
            Class<?>[] ints = new Class<?>[] {int.class, int.class, int.class};
            Ref.call(virtualDisplay, "resize", ints, new Object[] {encW, encH, dpi});
            Class<?> surfaceClass = Ref.cls("android.view.Surface");
            Ref.call(virtualDisplay, "setSurface", new Class<?>[] {surfaceClass}, new Object[] {surface});
            return;
        }
        if (displayToken != null) {
            project(surface, deviceW, deviceH, encW, encH, layerStack);
        }
    }

    void close() {
        if (virtualDisplay != null) {
            try {
                Ref.call(virtualDisplay, "release", new Class<?>[0], new Object[0]);
            } catch (Throwable ignored) {
            }
            virtualDisplay = null;
        }
        if (displayToken != null) {
            destroyToken(displayToken);
            displayToken = null;
        }
        surfaceControl = null;
    }

    private void openDisplayManager(Object context, Object surface, int encW, int encH, int dpi)
            throws Exception {
        Object manager = Ref.call(context, "getSystemService",
                new Class<?>[] {String.class}, new Object[] {"display"});
        if (manager == null) {
            throw new IllegalStateException("display service missing");
        }
        Class<?> surfaceClass = Ref.cls("android.view.Surface");
        Method create = Ref.method(manager.getClass(), "createVirtualDisplay",
                new Class<?>[] {String.class, int.class, int.class, int.class, surfaceClass, int.class});
        int[] flags = new int[] {FLAG_PUBLIC | FLAG_AUTO_MIRROR, FLAG_AUTO_MIRROR, FLAG_PUBLIC};
        Exception last = null;
        for (int flag : flags) {
            try {
                Object display = Ref.invoke(create, manager,
                        new Object[] {"miror", encW, encH, dpi, surface, flag});
                if (display == null) {
                    throw new IllegalStateException("createVirtualDisplay returned null");
                }
                virtualDisplay = display;
                return;
            } catch (Exception ex) {
                last = ex;
                Log.line("createVirtualDisplay flags=" + flag + " " + ex.getClass().getSimpleName()
                        + ": " + ex.getMessage());
            }
        }
        throw last == null ? new IllegalStateException("createVirtualDisplay failed") : last;
    }

    private void openSurfaceControl(Object surface, int deviceW, int deviceH, int encW, int encH,
            int layerStack, boolean fromServer) throws Exception {
        Object token;
        if (fromServer) {
            token = createServerDisplay("miror");
            surfaceControl = null;
        } else {
            surfaceControl = Ref.cls("android.view.SurfaceControl");
            token = Ref.callStatic((Class<?>) surfaceControl, "createDisplay",
                    new Class<?>[] {String.class, boolean.class},
                    new Object[] {"miror", Boolean.FALSE});
        }
        if (token == null) {
            throw new IllegalStateException("display token missing");
        }
        displayToken = token;
        project(surface, deviceW, deviceH, encW, encH, layerStack);
    }

    private void project(Object surface, int deviceW, int deviceH, int encW, int encH, int layerStack)
            throws Exception {
        Class<?> surfaceClass = Ref.cls("android.view.Surface");
        Class<?> binder = Ref.cls("android.os.IBinder");
        Class<?> rectClass = Ref.cls("android.graphics.Rect");
        Object layerRect = rectClass.getConstructor(int.class, int.class, int.class, int.class)
                .newInstance(0, 0, deviceW, deviceH);
        Object displayRect = rectClass.getConstructor(int.class, int.class, int.class, int.class)
                .newInstance(0, 0, encW, encH);
        Class<?> transactionClass = Ref.cls("android.view.SurfaceControl$Transaction");
        Object transaction = transactionClass.getConstructor().newInstance();
        Ref.call(transaction, "setDisplaySurface",
                new Class<?>[] {binder, surfaceClass}, new Object[] {displayToken, surface});
        Ref.call(transaction, "setDisplayProjection",
                new Class<?>[] {binder, int.class, rectClass, rectClass},
                new Object[] {displayToken, 0, layerRect, displayRect});
        Ref.call(transaction, "setDisplayLayerStack",
                new Class<?>[] {binder, int.class}, new Object[] {displayToken, layerStack});
        Ref.call(transaction, "apply", new Class<?>[0], new Object[0]);
        try {
            Ref.call(transaction, "close", new Class<?>[0], new Object[0]);
        } catch (Throwable ignored) {
        }
    }

    private Object createServerDisplay(String name) throws Exception {
        String classpath = System.getenv("SYSTEMSERVERCLASSPATH");
        if (classpath == null || classpath.length() == 0) {
            classpath = "/system/framework/services.jar";
        }
        Class<?> loaderClass = Class.forName("dalvik.system.PathClassLoader");
        ClassLoader loader = (ClassLoader) loaderClass
                .getConstructor(String.class, ClassLoader.class)
                .newInstance(classpath, ClassLoader.getSystemClassLoader());
        Class<?> displayControl = Class.forName("com.android.server.display.DisplayControl", true, loader);
        serverDisplayControl = displayControl;
        loadServerLibrary(displayControl);
        try {
            Method method = displayControl.getDeclaredMethod("createVirtualDisplay", String.class, boolean.class);
            method.setAccessible(true);
            return Ref.invoke(method, null, new Object[] {name, Boolean.FALSE});
        } catch (NoSuchMethodException ignored) {
            Method method = displayControl.getDeclaredMethod(
                    "createVirtualDisplay", String.class, boolean.class, String.class, float.class);
            method.setAccessible(true);
            return Ref.invoke(method, null, new Object[] {name, Boolean.FALSE, "miror", 0f});
        }
    }

    private static void loadServerLibrary(Class<?> owner) throws Exception {
        Method[] methods = Runtime.class.getDeclaredMethods();
        for (Method method : methods) {
            if (!"loadLibrary0".equals(method.getName())) {
                continue;
            }
            method.setAccessible(true);
            Class<?>[] params = method.getParameterTypes();
            try {
                if (params.length == 2 && params[0] == Class.class && params[1] == String.class) {
                    method.invoke(Runtime.getRuntime(), owner, "android_servers");
                    return;
                }
                if (params.length == 2 && params[1] == String.class) {
                    method.invoke(Runtime.getRuntime(), owner.getClassLoader(), "android_servers");
                    return;
                }
            } catch (Throwable ignored) {
            }
        }
        System.loadLibrary("android_servers");
    }

    private void destroyToken(Object token) {
        try {
            Class<?> binder = Ref.cls("android.os.IBinder");
            if (surfaceControl != null) {
                Ref.callStatic((Class<?>) surfaceControl, "destroyDisplay",
                        new Class<?>[] {binder}, new Object[] {token});
                return;
            }
        } catch (Throwable ignored) {
        }
        try {
            Class<?> binder = Ref.cls("android.os.IBinder");
            Class<?> displayControl = serverDisplayControl;
            if (displayControl == null) {
                displayControl = Class.forName("com.android.server.display.DisplayControl");
            }
            Ref.callStatic(displayControl, "destroyVirtualDisplay",
                    new Class<?>[] {binder}, new Object[] {token});
        } catch (Throwable ignored) {
        }
    }

    private static Exception asException(Throwable ex) {
        if (ex instanceof Exception) {
            return (Exception) ex;
        }
        return new Exception(ex);
    }
}
