// Relative mouse movement for offline test captures (camera pans). DSR binds the camera to the mouse.
using System;
using System.Runtime.InteropServices;
using System.Threading;

public static class DsrMouse
{
    [StructLayout(LayoutKind.Sequential)]
    struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    struct INPUT { public uint type; public MOUSEINPUT mi; }

    [DllImport("user32.dll", SetLastError = true)]
    static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    const uint INPUT_MOUSE = 0;
    const uint MOUSEEVENTF_MOVE = 0x0001;

    public static void Move(int dx, int dy)
    {
        INPUT[] i = new INPUT[1];
        i[0].type = INPUT_MOUSE;
        i[0].mi.dx = dx;
        i[0].mi.dy = dy;
        i[0].mi.dwFlags = MOUSEEVENTF_MOVE;
        SendInput(1, i, Marshal.SizeOf(typeof(INPUT)));
    }

    static volatile bool running;
    static Thread worker;

    // Sends (dx, 0) every stepMs on a background thread until StopPan(): a constant-rate camera pan.
    public static void StartPan(int dx, int stepMs)
    {
        StopPan();
        running = true;
        worker = new Thread(() => { while (running) { Move(dx, 0); Thread.Sleep(stepMs); } });
        worker.IsBackground = true;
        worker.Start();
    }

    public static void StopPan()
    {
        running = false;
        if (worker != null) { worker.Join(); worker = null; }
    }
}
