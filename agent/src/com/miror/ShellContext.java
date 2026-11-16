package com.miror;

import android.content.Context;
import android.content.ContextWrapper;

/** Context whose package name is the shell uid, so display APIs accept the call. */
public final class ShellContext extends ContextWrapper {
    public ShellContext(Context base) {
        super(base);
    }

    @Override
    public String getPackageName() {
        return "com.android.shell";
    }

    @Override
    public String getOpPackageName() {
        return "com.android.shell";
    }
}
