package android.content;

/** Compile-time stub. The device framework supplies the real class. */
public class ContextWrapper extends Context {
    public ContextWrapper(Context base) {
    }

    @Override
    public Object getSystemService(String name) {
        return null;
    }

    @Override
    public String getPackageName() {
        return null;
    }

    @Override
    public String getOpPackageName() {
        return null;
    }
}
