package com.miror;

import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

final class Ref {
    private Ref() {
    }

    static Class<?> cls(String name) throws ClassNotFoundException {
        return Class.forName(name);
    }

    static Object call(Object target, String name, Class<?>[] types, Object[] args) throws Exception {
        return invoke(method(target.getClass(), name, types), target, args);
    }

    static Object callStatic(Class<?> type, String name, Class<?>[] types, Object[] args) throws Exception {
        return invoke(method(type, name, types), null, args);
    }

    static Object invoke(Method method, Object target, Object[] args) throws Exception {
        try {
            return method.invoke(target, args);
        } catch (InvocationTargetException ex) {
            Throwable cause = ex.getCause();
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            if (cause instanceof Error) {
                throw (Error) cause;
            }
            throw ex;
        }
    }

    static Method method(Class<?> type, String name, Class<?>[] types) throws NoSuchMethodException {
        try {
            Method found = type.getMethod(name, types);
            found.setAccessible(true);
            return found;
        } catch (NoSuchMethodException first) {
            Class<?> cursor = type;
            while (cursor != null) {
                try {
                    Method found = cursor.getDeclaredMethod(name, types);
                    found.setAccessible(true);
                    return found;
                } catch (NoSuchMethodException ignored) {
                    cursor = cursor.getSuperclass();
                }
            }
            throw first;
        }
    }

    static int intField(Object target, String name) throws Exception {
        Field field = field(target.getClass(), name);
        Object value = field.get(target);
        if (value instanceof Number) {
            return Math.round(((Number) value).floatValue());
        }
        throw new NoSuchFieldException(name);
    }

    static float floatField(Object target, String name) throws Exception {
        Field field = field(target.getClass(), name);
        Object value = field.get(target);
        if (value instanceof Number) {
            return ((Number) value).floatValue();
        }
        throw new NoSuchFieldException(name);
    }

    static Field field(Class<?> type, String name) throws NoSuchFieldException {
        Class<?> cursor = type;
        while (cursor != null) {
            try {
                Field found = cursor.getDeclaredField(name);
                found.setAccessible(true);
                return found;
            } catch (NoSuchFieldException ignored) {
                cursor = cursor.getSuperclass();
            }
        }
        throw new NoSuchFieldException(name);
    }
}
