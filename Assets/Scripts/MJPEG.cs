using UnityEngine;
using System.Collections;
using System.Net;
using System.IO;
using System.Threading;

public class MJPEGStream : MonoBehaviour
{
    public Renderer screen;
    public string publicUrl =
        "http://JETSON_IP_HERE:8080/stream";

    private const int READ_TIMEOUT_MS = 1000;
    private const int CONNECT_TIMEOUT_MS = 3000;
    private const int RETRY_DELAY_MS = 1000;
    private const int FRAME_BUFFER_SIZE = 300000;
    private const int READ_BUFFER_SIZE = 32768;

    private Texture2D _tex;
    private volatile byte[] _latestFrame;
    private volatile bool _threadRunning = false;
    private Thread _streamThread;

    void Start()
    {
        ALog("MJPEGStream", "Start()");

        Application.runInBackground = true;
        Application.targetFrameRate = 72;
        QualitySettings.vSyncCount = 0;

        if (screen == null)
        {
            ALog("MJPEGStream", "ERROR screen NULL");
            return;
        }

        // Use a new material instance so swapping
        // in ModeManager doesn't break our reference
        screen.material = new Material(screen.material);

        _tex = new Texture2D(2, 2,
                              TextureFormat.RGB24, false);
        _tex.wrapMode = TextureWrapMode.Clamp;
        _tex.filterMode = FilterMode.Bilinear;

        screen.material.mainTexture = _tex;

        StartStreamThread();
        StartCoroutine(ApplyFrames());
    }

    // ── Called by ModeManager after material swap ─────────────
    public void ReassignTexture()
    {
        if (screen != null && _tex != null)
        {
            screen.material.mainTexture = _tex;
            ALog("MJPEGStream", "Texture reassigned");
        }
    }

    // ── Called by ModeManager only if truly needed ────────────
    public void RestartStream()
    {
        ALog("MJPEGStream", "RestartStream()");
        StopStreamThread();
        StartCoroutine(DelayedStart(0.3f));
    }

    IEnumerator DelayedStart(float t)
    {
        yield return new WaitForSeconds(t);
        StartStreamThread();
    }

    void StartStreamThread()
    {
        if (_threadRunning)
        {
            ALog("MJPEGStream", "Already running");
            return;
        }
        _threadRunning = true;
        _streamThread = new Thread(StreamThread)
        { IsBackground = true };
        _streamThread.Start();
        ALog("MJPEGStream", $"Thread started → {publicUrl}");
    }

    void StopStreamThread()
    {
        _threadRunning = false;
        ALog("MJPEGStream", "Thread stop requested");
    }

    void StreamThread()
    {
        while (_threadRunning)
        {
            HttpWebResponse response = null;
            Stream stream = null;
            try
            {
                ALog("MJPEGStream", "Connecting...");
                var req =
                    (HttpWebRequest)WebRequest.Create(publicUrl);
                req.Timeout = CONNECT_TIMEOUT_MS;
                req.KeepAlive = false;
                req.ProtocolVersion = HttpVersion.Version10;

                response = (HttpWebResponse)req.GetResponse();
                stream = response.GetResponseStream();
                stream.ReadTimeout = READ_TIMEOUT_MS;

                ALog("MJPEGStream", "Connected OK");

                byte[] readBuf = new byte[READ_BUFFER_SIZE];
                byte[] frameBuf = new byte[FRAME_BUFFER_SIZE];
                int fIdx = 0;
                bool cap = false;

                while (_threadRunning)
                {
                    int n = stream.Read(
                        readBuf, 0, readBuf.Length);
                    if (n <= 0) break;

                    for (int i = 0; i < n; i++)
                    {
                        if (!cap &&
                            i < n - 1 &&
                            readBuf[i] == 0xFF &&
                            readBuf[i + 1] == 0xD8)
                        { cap = true; fIdx = 0; }

                        if (cap)
                        {
                            if (fIdx < frameBuf.Length)
                                frameBuf[fIdx++] = readBuf[i];

                            if (i < n - 1 &&
                                readBuf[i] == 0xFF &&
                                readBuf[i + 1] == 0xD9)
                            {
                                frameBuf[fIdx++] = readBuf[i + 1];
                                var f = new byte[fIdx];
                                System.Buffer.BlockCopy(
                                    frameBuf, 0, f, 0, fIdx);
                                _latestFrame = f;
                                cap = false; i++;
                            }
                        }
                    }
                }
            }
            catch (System.Exception e)
            {
                ALog("MJPEGStream", $"Ex: {e.Message}");
            }
            finally
            {
                try { stream?.Close(); } catch { }
                try { response?.Close(); } catch { }
            }

            if (_threadRunning)
                Thread.Sleep(RETRY_DELAY_MS);
        }
        ALog("MJPEGStream", "Thread exited");
    }

    IEnumerator ApplyFrames()
    {
        while (true)
        {
            if (_latestFrame != null)
            {
                byte[] f = _latestFrame;
                _latestFrame = null;
                if (_tex != null)
                    _tex.LoadImage(f, false);

                // Always reassign in case material changed
                if (screen != null && _tex != null)
                    screen.material.mainTexture = _tex;
            }
            yield return null;
        }
    }

    // ── IMPORTANT: do NOT stop thread on disable ──────────────
    // ModeManager hides the quad by swapping material,
    // not by disabling — so OnDisable never fires.
    // But keep OnDestroy for clean app exit.
    void OnDestroy()
    {
        StopStreamThread();
        if (_tex != null) Destroy(_tex);
    }

    static void ALog(string tag, string msg)
    {
        Debug.Log($"[{tag}] {msg}");
#if UNITY_ANDROID && !UNITY_EDITOR
        try
        {
            using var c = new AndroidJavaClass(
                "android.util.Log");
            c.CallStatic<int>("d", tag, msg);
        }
        catch { }
#endif
    }
}