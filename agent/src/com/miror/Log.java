package com.miror;

final class Log {
    private Log() {
    }

    static void line(String message) {
        System.out.println("MIROR_LOG " + message);
        System.out.flush();
        try {
            Class.forName("android.util.Log")
                    .getMethod("i", String.class, String.class)
                    .invoke(null, "miror", message);
        } catch (Throwable ignored) {
        }
    }
}
