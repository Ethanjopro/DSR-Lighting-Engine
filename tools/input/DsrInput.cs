// Scan-code keyboard input, focus forcing and window capture for driving DSR in tests.
// Compiled at runtime by PowerShell 5.1 Add-Type (C# 5): no string interpolation, no expression bodies.
using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Threading;

public static class DsrInput
{
    [StructLayout(LayoutKind.Sequential)]
    struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Explicit)]
    struct InputUnion { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
    [StructLayout(LayoutKind.Sequential)]
    struct INPUT { public uint type; public InputUnion u; }
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    struct POINT { public int X, Y; }

    const uint INPUT_MOUSE = 0;
    const uint MOUSEEVENTF_MOVE = 0x1;
    const uint INPUT_KEYBOARD = 1;
    const uint KEYEVENTF_EXTENDEDKEY = 0x1;
    const uint KEYEVENTF_KEYUP = 0x2;
    const uint KEYEVENTF_SCANCODE = 0x8;
    const int SW_RESTORE = 9;
    const uint PW_RENDERFULLCONTENT = 2;

    [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hWnd, ref POINT p);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();

    // Without this, a 150%-scaled 4K desktop is virtualized to 2560x1440 and captures are cropped.
    public static void EnsureDpiAware()
    {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch (EntryPointNotFoundException) { }
        SetProcessDPIAware();
    }

    public static IntPtr FindGameWindow()
    {
        foreach (Process p in Process.GetProcessesByName("DarkSoulsRemastered"))
        {
            p.Refresh();
            if (p.MainWindowHandle != IntPtr.Zero) return p.MainWindowHandle;
        }
        return IntPtr.Zero;
    }

    public static bool Focus(IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero) return false;
        if (GetForegroundWindow() == hWnd) return true;
        uint fg = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
        uint me = GetCurrentThreadId();
        AttachThreadInput(me, fg, true);
        ShowWindow(hWnd, SW_RESTORE);
        BringWindowToTop(hWnd);
        SetForegroundWindow(hWnd);
        AttachThreadInput(me, fg, false);
        Thread.Sleep(150);
        return GetForegroundWindow() == hWnd;
    }

    public static bool IsForeground(IntPtr hWnd) { return GetForegroundWindow() == hWnd; }

    // dik: DirectInput key code as stored in DarkSoulsKeys.ini (>= 0x80 means extended, e.g. 200 = DIK_UP).
    static INPUT Key(int dik, bool up)
    {
        INPUT i = new INPUT();
        i.type = INPUT_KEYBOARD;
        i.u.ki.wScan = (ushort)(dik & 0x7F);
        i.u.ki.dwFlags = KEYEVENTF_SCANCODE | (dik >= 0x80 ? KEYEVENTF_EXTENDEDKEY : 0) | (up ? KEYEVENTF_KEYUP : 0);
        return i;
    }

    public static void Tap(int dik, int holdMs)
    {
        INPUT[] down = { Key(dik, false) };
        INPUT[] up = { Key(dik, true) };
        SendInput(1, down, Marshal.SizeOf(typeof(INPUT)));
        Thread.Sleep(holdMs);
        SendInput(1, up, Marshal.SizeOf(typeof(INPUT)));
    }

    // Relative mouse motion spread across frames. The game's camera is bound to the mouse axes, not to keys.
    public static void MoveMouse(int dx, int dy, int steps, int gapMs)
    {
        int doneX = 0, doneY = 0;
        for (int s = 1; s <= steps; s++)
        {
            INPUT[] m = { new INPUT() };
            m[0].type = INPUT_MOUSE;
            m[0].u.mi.dx = dx * s / steps - doneX;
            m[0].u.mi.dy = dy * s / steps - doneY;
            m[0].u.mi.dwFlags = MOUSEEVENTF_MOVE;
            doneX += m[0].u.mi.dx;
            doneY += m[0].u.mi.dy;
            SendInput(1, m, Marshal.SizeOf(typeof(INPUT)));
            Thread.Sleep(gapMs);
        }
    }

    public static RECT ClientScreenRect(IntPtr hWnd)
    {
        RECT c; GetClientRect(hWnd, out c);
        POINT p = new POINT(); ClientToScreen(hWnd, ref p);
        RECT r; r.Left = p.X; r.Top = p.Y; r.Right = p.X + c.Right; r.Bottom = p.Y + c.Bottom;
        return r;
    }

    // mode "screen": copy the composed desktop under the client area; "print": PrintWindow full content.
    public static Bitmap Grab(IntPtr hWnd, string mode)
    {
        RECT r = ClientScreenRect(hWnd);
        int w = r.Right - r.Left, h = r.Bottom - r.Top;
        Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
        using (Graphics g = Graphics.FromImage(bmp))
        {
            if (mode == "print")
            {
                IntPtr hdc = g.GetHdc();
                PrintWindow(hWnd, hdc, PW_RENDERFULLCONTENT);
                g.ReleaseHdc(hdc);
            }
            else
            {
                g.CopyFromScreen(r.Left, r.Top, 0, 0, new Size(w, h), CopyPixelOperation.SourceCopy);
            }
        }
        return bmp;
    }

    public static void Save(Bitmap bmp, string path) { bmp.Save(path, ImageFormat.Png); }

    // Mean luma (0-255) on a coarse grid; cheap screen-state probe (black loading screens, fades).
    public static double MeanLuma(Bitmap bmp)
    {
        double sum = 0; int n = 0;
        int stepX = Math.Max(1, bmp.Width / 96), stepY = Math.Max(1, bmp.Height / 54);
        for (int y = stepY / 2; y < bmp.Height; y += stepY)
            for (int x = stepX / 2; x < bmp.Width; x += stepX)
            {
                Color c = bmp.GetPixel(x, y);
                sum += 0.2126 * c.R + 0.7152 * c.G + 0.0722 * c.B; n++;
            }
        return n > 0 ? sum / n : 0;
    }

    static byte[] Pixels(Bitmap bmp, out int stride)
    {
        Rectangle r = new Rectangle(0, 0, bmp.Width, bmp.Height);
        BitmapData d = bmp.LockBits(r, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
        stride = d.Stride;
        byte[] buf = new byte[d.Stride * d.Height];
        Marshal.Copy(d.Scan0, buf, 0, buf.Length);
        bmp.UnlockBits(d);
        return buf;
    }

    // Per-pixel, per-channel median of three frames (removes particle/flicker noise).
    public static Bitmap Median3(Bitmap a, Bitmap b, Bitmap c)
    {
        int stride;
        byte[] pa = Pixels(a, out stride), pb = Pixels(b, out stride), pc = Pixels(c, out stride);
        byte[] o = new byte[pa.Length];
        for (int i = 0; i < pa.Length; i++)
        {
            byte x = pa[i], y = pb[i], z = pc[i];
            o[i] = Math.Max(Math.Min(x, y), Math.Min(Math.Max(x, y), z));
        }
        Bitmap res = new Bitmap(a.Width, a.Height, PixelFormat.Format32bppArgb);
        BitmapData d = res.LockBits(new Rectangle(0, 0, a.Width, a.Height), ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
        Marshal.Copy(o, 0, d.Scan0, o.Length);
        res.UnlockBits(d);
        return res;
    }

    static double Cbrt(double v) { return v <= 0 ? 0 : Math.Pow(v, 1.0 / 3.0); } // .NET Framework has no Math.Cbrt

    static double SrgbToLinear(double c) { return c <= 0.04045 ? c / 12.92 : Math.Pow((c + 0.055) / 1.055, 2.4); }

    // Image statistics for look guardrails. masks = flat [x0,y0,x1,y1,...] in pixel coords (HUD regions).
    // Returns JSON: log-average luminance (EV), luma percentiles, clip/crush %, saturation, Oklab midtone a/b.
    public static string StatsJson(Bitmap bmp, int[] masks)
    {
        int stride; byte[] p = Pixels(bmp, out stride);
        double[] lin = new double[256];
        for (int i = 0; i < 256; i++) lin[i] = SrgbToLinear(i / 255.0);
        int[] hist = new int[256];
        double logSum = 0, satSum = 0, aSum = 0, bSum = 0; long n = 0, clip = 0, crush = 0, mid = 0;
        int step = 2; // every other pixel in each axis: 2M samples at 4K
        for (int y = 0; y < bmp.Height; y += step)
        {
            for (int x = 0; x < bmp.Width; x += step)
            {
                bool masked = false;
                for (int m = 0; m + 3 < masks.Length; m += 4)
                    if (x >= masks[m] && y >= masks[m + 1] && x < masks[m + 2] && y < masks[m + 3]) { masked = true; break; }
                if (masked) continue;
                int o = y * stride + x * 4;
                int B = p[o], G = p[o + 1], R = p[o + 2];
                double r = lin[R], g = lin[G], b = lin[B];
                double Y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
                logSum += Math.Log(Math.Max(Y, 1e-5));
                int luma8 = (int)Math.Round(0.2126 * R + 0.7152 * G + 0.0722 * B);
                hist[Math.Min(255, Math.Max(0, luma8))]++;
                if (R >= 250 || G >= 250 || B >= 250) clip++;
                if (luma8 <= 3) crush++;
                int mx = Math.Max(R, Math.Max(G, B)), mn = Math.Min(R, Math.Min(G, B));
                satSum += mx > 0 ? (mx - mn) / (double)mx : 0;
                if (luma8 >= 40 && luma8 <= 200)
                {
                    double l_ = Cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
                    double m_ = Cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
                    double s_ = Cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
                    aSum += 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_;
                    bSum += 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_;
                    mid++;
                }
                n++;
            }
        }
        Func<double, int> pct = delegate (double q) {
            long target = (long)(q * n), acc = 0;
            for (int i = 0; i < 256; i++) { acc += hist[i]; if (acc >= target) return i; }
            return 255;
        };
        double logAvg = Math.Exp(logSum / Math.Max(1, n));
        System.Globalization.CultureInfo ci = System.Globalization.CultureInfo.InvariantCulture;
        // Closing brace appended outside Format: "{12:F4}}}" is mis-parsed as a format spec.
        return string.Format(ci,
            "{{\"samples\":{0},\"log_avg_lum\":{1:F6},\"ev\":{2:F4},\"p10\":{3},\"p50\":{4},\"p90\":{5},\"p99\":{6}," +
            "\"clip_pct\":{7:F3},\"crush_pct\":{8:F3},\"mean_sat\":{9:F4},\"mid_oklab_a\":{10:F5},\"mid_oklab_b\":{11:F5},\"mid_frac\":{12:F4}",
            n, logAvg, Math.Log(logAvg, 2), pct(0.10), pct(0.50), pct(0.90), pct(0.99),
            100.0 * clip / Math.Max(1, n), 100.0 * crush / Math.Max(1, n), satSum / Math.Max(1, n),
            mid > 0 ? aSum / mid : 0, mid > 0 ? bSum / mid : 0, mid / (double)Math.Max(1, n)) + "}";
    }

    public static Color PixelAt(Bitmap bmp, int x, int y) { return bmp.GetPixel(Math.Min(x, bmp.Width - 1), Math.Min(y, bmp.Height - 1)); }

    // Small downscaled copy for quick visual checks.
    public static Bitmap Thumb(Bitmap bmp, int width)
    {
        int h = (int)Math.Round(bmp.Height * (width / (double)bmp.Width));
        Bitmap t = new Bitmap(width, h);
        using (Graphics g = Graphics.FromImage(t))
        {
            g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBilinear;
            g.DrawImage(bmp, 0, 0, width, h);
        }
        return t;
    }
}
