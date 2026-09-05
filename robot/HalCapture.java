import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.CaptureResult;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.util.Range;
import android.util.Size;

import java.io.FileOutputStream;
import java.lang.reflect.Constructor;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Capture one JPEG from the Pixel's rear camera at an explicit zoom ratio.
 *
 * Termux:API's `termux-camera-photo` always shoots the logical rear camera at
 * 1x, which pins it to the main lens. A zoom ratio below 1.0 is what makes the
 * framework hand the request to the ultra-wide physical sensor, and camera2
 * only exposes that from a real Android runtime -- hence app_process rather
 * than a plain native binary.
 */
public class HalCapture {

    private static final String CAMERA_PACKAGE = "com.termux.api";

    /** Minimal stand-in for an app context, in the shape scrcpy uses. */
    static class FakeContext extends ContextWrapper {
        private final String pkg;

        FakeContext(Context base, String pkg) {
            super(base);
            this.pkg = pkg;
        }

        @Override public String getPackageName() { return pkg; }

        @Override public String getOpPackageName() { return pkg; }

        @Override
        public AttributionSource getAttributionSource() {
            return new AttributionSource.Builder(Process.myUid())
                    .setPackageName(pkg)
                    .build();
        }
    }

    private static final Object LOCK = new Object();
    private static byte[] captured;
    private static boolean armed;
    private static CountDownLatch done = new CountDownLatch(1);
    private static final CountDownLatch metadata = new CountDownLatch(1);
    private static String activeLens = "?";

    public static void main(String[] args) {
        String cameraId = "0";
        String out = null;
        float zoom = -1f;          // negative means "widest the camera allows"
        int wantW = 1280, wantH = 960, settleMs = 800, timeoutMs = 15000;

        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("-c") && i + 1 < args.length) cameraId = args[++i];
            else if (args[i].equals("-z") && i + 1 < args.length) {
                String v = args[++i];
                zoom = v.equals("widest") ? -1f : Float.parseFloat(v);
            } else if (args[i].equals("-w") && i + 1 < args.length) wantW = Integer.parseInt(args[++i]);
            else if (args[i].equals("-h") && i + 1 < args.length) wantH = Integer.parseInt(args[++i]);
            else if (args[i].equals("--settle-ms") && i + 1 < args.length) settleMs = Integer.parseInt(args[++i]);
            else if (args[i].equals("--timeout-ms") && i + 1 < args.length) timeoutMs = Integer.parseInt(args[++i]);
            else out = args[i];
        }
        if (out == null) {
            System.err.println("usage: HalCapture [-c id] [-z zoom|widest] [-w W] [-h H] out.jpg");
            System.exit(2);
        }

        try {
            capture(cameraId, zoom, wantW, wantH, settleMs, timeoutMs, out);
        } catch (Throwable t) {
            t.printStackTrace();
            System.exit(1);
        }
        System.exit(0);
    }

    private static void capture(String cameraId, float zoom, int wantW, int wantH,
                                int settleMs, int timeoutMs, String out) throws Exception {
        Looper.prepareMainLooper();
        Class<?> at = Class.forName("android.app.ActivityThread");
        Object thread = at.getMethod("systemMain").invoke(null);
        Context sys = (Context) at.getMethod("getSystemContext").invoke(thread);
        // The system context reports uid 1000 / package "android", which the
        // camera service rejects for this process ("disabled by policy").
        // Present a package that actually owns our uid instead -- Termux:API
        // is the one holding the CAMERA grant for uid 10338.
        Context ctx = new FakeContext(sys, CAMERA_PACKAGE);
        Constructor<CameraManager> ctor = CameraManager.class.getDeclaredConstructor(Context.class);
        ctor.setAccessible(true);
        final CameraManager cm = ctor.newInstance(ctx);

        CameraCharacteristics chars = cm.getCameraCharacteristics(cameraId);
        float[] range = zoomRange(chars);
        if (zoom < 0) zoom = range[0];
        if (zoom < range[0]) zoom = range[0];
        if (zoom > range[1]) zoom = range[1];

        StreamConfigurationMap map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size size = pickSize(map.getOutputSizes(ImageFormat.JPEG), wantW, wantH);

        HandlerThread ht = new HandlerThread("hal-capture");
        ht.start();
        final Handler handler = new Handler(ht.getLooper());

        final ImageReader reader = ImageReader.newInstance(
                size.getWidth(), size.getHeight(), ImageFormat.JPEG, 3);
        reader.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
            @Override
            public void onImageAvailable(ImageReader r) {
                Image image = r.acquireLatestImage();
                if (image == null) return;
                try {
                    synchronized (LOCK) {
                        // Frames that land during the 3A settle window are
                        // throwaway; only keep one once the still is armed.
                        if (armed && captured == null) {
                            ByteBuffer buf = image.getPlanes()[0].getBuffer();
                            byte[] bytes = new byte[buf.remaining()];
                            buf.get(bytes);
                            captured = bytes;
                            done.countDown();
                        }
                    }
                } finally {
                    image.close();
                }
            }
        }, handler);

        final CountDownLatch opened = new CountDownLatch(1);
        final CameraDevice[] deviceRef = new CameraDevice[1];
        cm.openCamera(cameraId, new CameraDevice.StateCallback() {
            @Override public void onOpened(CameraDevice device) { deviceRef[0] = device; opened.countDown(); }
            @Override public void onDisconnected(CameraDevice device) { device.close(); opened.countDown(); }
            @Override public void onError(CameraDevice device, int error) {
                System.err.println("camera error " + error);
                device.close();
                opened.countDown();
            }
        }, handler);
        if (!opened.await(timeoutMs, TimeUnit.MILLISECONDS) || deviceRef[0] == null)
            throw new IllegalStateException("camera did not open");
        final CameraDevice device = deviceRef[0];

        final CountDownLatch configured = new CountDownLatch(1);
        final CameraCaptureSession[] sessionRef = new CameraCaptureSession[1];
        List<android.view.Surface> surfaces = new ArrayList<android.view.Surface>();
        surfaces.add(reader.getSurface());
        device.createCaptureSession(surfaces, new CameraCaptureSession.StateCallback() {
            @Override public void onConfigured(CameraCaptureSession s) { sessionRef[0] = s; configured.countDown(); }
            @Override public void onConfigureFailed(CameraCaptureSession s) { configured.countDown(); }
        }, handler);
        if (!configured.await(timeoutMs, TimeUnit.MILLISECONDS) || sessionRef[0] == null)
            throw new IllegalStateException("capture session did not configure");
        CameraCaptureSession session = sessionRef[0];

        // Let 3A converge on a repeating preview before the still: a frame
        // fired the instant the session opens comes out dark and unfocused.
        CaptureRequest.Builder preview = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
        preview.addTarget(reader.getSurface());
        applyZoom(preview, zoom);
        session.setRepeatingRequest(preview.build(), null, handler);
        Thread.sleep(settleMs);
        session.stopRepeating();
        Thread.sleep(120);

        CaptureRequest.Builder still = device.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
        still.addTarget(reader.getSurface());
        applyZoom(still, zoom);
        synchronized (LOCK) { armed = true; }
        // Report which physical lens actually served the frame: on a logical
        // multi-camera the zoom ratio is a request, and this is the only
        // honest confirmation that the ultra-wide picked it up.
        session.capture(still.build(), new CameraCaptureSession.CaptureCallback() {
            @Override
            public void onCaptureCompleted(CameraCaptureSession s, CaptureRequest r, TotalCaptureResult result) {
                activeLens = "physical_id=" + result.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID)
                        + " focal=" + result.get(CaptureResult.LENS_FOCAL_LENGTH)
                        + "mm applied_zoom=" + result.get(CaptureResult.CONTROL_ZOOM_RATIO);
                metadata.countDown();
            }
        }, handler);

        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS) || captured == null)
            throw new IllegalStateException("no frame captured");
        metadata.await(2000, TimeUnit.MILLISECONDS);

        FileOutputStream fos = new FileOutputStream(out);
        try { fos.write(captured); } finally { fos.close(); }
        System.err.println("hal-capture: " + size.getWidth() + "x" + size.getHeight()
                + " zoom=" + zoom + " -> " + captured.length + " bytes; " + activeLens);

        session.close();
        device.close();
        reader.close();
    }

    /** Zoom below 1.0 is what hands the request to the ultra-wide lens. */
    private static void applyZoom(CaptureRequest.Builder b, float zoom) {
        b.set(CaptureRequest.CONTROL_ZOOM_RATIO, Float.valueOf(zoom));
    }

    private static float[] zoomRange(CameraCharacteristics chars) {
        Range<Float> r = chars.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE);
        if (r == null) return new float[]{1f, 1f};
        return new float[]{r.getLower().floatValue(), r.getUpper().floatValue()};
    }

    /**
     * Smallest advertised JPEG size that still covers the request, preferring
     * 4:3 so the frame matches what HAL's pipeline resizes to. Keeping the
     * capture small keeps both the encode and the downstream resize cheap.
     */
    private static Size pickSize(Size[] sizes, int wantW, int wantH) {
        Size best = null, bestAny = null, largest = null;
        for (Size s : sizes) {
            if (largest == null || (long) s.getWidth() * s.getHeight()
                    > (long) largest.getWidth() * largest.getHeight()) largest = s;
            if (s.getWidth() < wantW || s.getHeight() < wantH) continue;
            long area = (long) s.getWidth() * s.getHeight();
            if (bestAny == null || area < (long) bestAny.getWidth() * bestAny.getHeight()) bestAny = s;
            boolean fourThree = Math.abs(s.getWidth() * 3 - s.getHeight() * 4) <= 8;
            if (!fourThree) continue;
            if (best == null || area < (long) best.getWidth() * best.getHeight()) best = s;
        }
        if (best != null) return best;
        if (bestAny != null) return bestAny;
        return largest;
    }
}
