using UnityEngine;
using Unity.XR.CoreUtils;
using System.Net.Sockets;
using System.Text;

public class HeadSender : MonoBehaviour
{
    [Header("XR")]
    public XROrigin xrOrigin;

    [Header("UDP")]
    public string jetsonIP = "JETSON_IP_HERE";
    public int port = 5005;

    [Header("Smoothing")]
    [Range(0.01f, 1f)]
    public float smoothing = 0.1f;

    [Header("Mode")]
    [Tooltip("Assign ModeManager here — gimbal only moves in Camera Feed mode.")]
    public ModeManager modeManager;

    [Header("Overlay sync")]
    [Tooltip("Assign DetectionOverlay here — gimbal angles are forwarded so " +
             "outlines follow the physical camera rotation.")]
    public DetectionOverlay detectionOverlay;

    private UdpClient client;
    private float smoothYaw = 0f;
    private float smoothPitch = 0f;
    private float baseYaw = 0f;
    private float basePitch = 0f;
    private bool calibrated = false;
    private bool _wasPassthrough = true;

    void Start()
    {
        try
        {
            client = new UdpClient();
            client.Connect(jetsonIP, port);
            Debug.Log("[HEAD] UDP client connected to " + jetsonIP + ":" + port);
        }
        catch (System.Exception e)
        {
            Debug.LogError("[HEAD] Failed to create UDP client: " + e.Message);
        }
    }

    void Update()
    {
        if (client == null) { Debug.LogError("[HEAD] UDP client is null!"); return; }
        if (xrOrigin == null) { Debug.LogError("[HEAD] XR Origin missing!"); return; }
        if (xrOrigin.Camera == null) { Debug.LogError("[HEAD] XR Camera missing!"); return; }

        bool isPassthrough = modeManager == null ? false : modeManager.IsPassthrough;

        if (!_wasPassthrough && isPassthrough)
            Debug.Log("[HEAD] Switched to Passthrough — gimbal frozen");

        _wasPassthrough = isPassthrough;

        if (isPassthrough) return;

        Transform headset = xrOrigin.Camera.transform;
        Vector3 forward = headset.forward;

        float yaw = Mathf.Atan2(forward.x, forward.z) * Mathf.Rad2Deg;
        float pitch = Mathf.Asin(Mathf.Clamp(forward.y, -1f, 1f)) * Mathf.Rad2Deg;

        if (!calibrated)
        {
            baseYaw = yaw;
            basePitch = pitch;
            calibrated = true;
            Debug.Log($"[HEAD] CALIBRATED — base Yaw={baseYaw:F1} Pitch={basePitch:F1}");
        }

        float relativeYaw = Mathf.DeltaAngle(baseYaw, yaw);
        float relativePitch = pitch - basePitch;

        smoothYaw = Mathf.Lerp(smoothYaw, relativeYaw, smoothing);
        smoothPitch = Mathf.Lerp(smoothPitch, relativePitch, smoothing);

        SendGimbal(smoothYaw, smoothPitch);

        if (detectionOverlay != null)
            detectionOverlay.SetGimbalAngles(smoothYaw, smoothPitch);
    }

    void SendGimbal(float yaw, float pitch)
    {
        string msg = yaw.ToString("F2") + "," + pitch.ToString("F2");
        byte[] data = Encoding.UTF8.GetBytes(msg);
        try
        {
            client.Send(data, data.Length);
            Debug.Log("[HEAD] Sent: " + msg);
        }
        catch (System.Exception e)
        {
            Debug.LogError("[HEAD] Send failed: " + e.Message);
        }
    }

    void OnApplicationQuit()
    {
        SendGimbal(0f, 0f);
        if (client != null) client.Close();
    }
}