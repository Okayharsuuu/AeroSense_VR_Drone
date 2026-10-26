using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;

public class ModeManager : MonoBehaviour
{
    [Header("Scene References")]
    public GameObject videoScreenQuad;
    public OVRPassthroughLayer passthroughLayer;
    public Camera mainCamera;
    public DetectionOverlay detectionOverlay;

    [Header("Controller Button")]
    [Tooltip("Press this button to toggle between Passthrough and Camera Feed.")]
    public OVRInput.Button toggleButton = OVRInput.Button.One;   // A button

    private const float TOGGLE_COOLDOWN = 1.0f;
    private float _lastToggleTime = -99f;

    public bool IsPassthrough { get; private set; } = true;

    private Renderer _quadRenderer;
    private Material _visibleMat;
    private Material _hiddenMat;
    private MJPEGStream _mjpeg;

    void Start()
    {
        ALog("ModeManager", "Start()");

        if (videoScreenQuad != null)
        {
            _quadRenderer = videoScreenQuad.GetComponent<Renderer>();
            _mjpeg = videoScreenQuad.GetComponent<MJPEGStream>();

            if (_quadRenderer != null)
            {
                _visibleMat = _quadRenderer.material;

                _hiddenMat = new Material(_visibleMat);
                _hiddenMat.color = new Color(0, 0, 0, 0);
                _hiddenMat.SetFloat("_Surface", 1);
                _hiddenMat.SetFloat("_Blend", 0);
                _hiddenMat.SetInt("_ZWrite", 0);
                _hiddenMat.renderQueue = 1;  // render first, before everything, writes nothing
            }
        }

        if (passthroughLayer != null)
            passthroughLayer.enabled = false;

        // Start in passthrough mode
        IsPassthrough = true;
        ApplyMode();
    }

    void Update()
    {
        if (OVRInput.GetDown(toggleButton))
        {
            float now = Time.time;
            if (now - _lastToggleTime < TOGGLE_COOLDOWN)
            {
                ALog("ModeManager", "Toggle ignored — cooldown");
                return;
            }
            _lastToggleTime = now;
            Toggle();
        }
    }

    public void Toggle()
    {
        IsPassthrough = !IsPassthrough;
        ALog("ModeManager", $"Toggle → {(IsPassthrough ? "PASSTHROUGH" : "CAMERA FEED")}");
        ApplyMode();
    }

    void ApplyMode()
    {
        if (IsPassthrough)
            EnterPassthrough();
        else
            EnterCameraFeed();

        if (detectionOverlay != null)
            detectionOverlay.SetPassthroughMode(IsPassthrough);
    }

    void EnterPassthrough()
    {
        ALog("ModeManager", "EnterPassthrough");

        if (_quadRenderer != null && _hiddenMat != null)
            _quadRenderer.material = _hiddenMat;

        if (passthroughLayer != null)
            passthroughLayer.enabled = true;

        if (mainCamera != null)
        {
            mainCamera.clearFlags = CameraClearFlags.SolidColor;
            mainCamera.backgroundColor = new Color(0, 0, 0, 0);
        }
    }

    void EnterCameraFeed()
    {
        ALog("ModeManager", "EnterCameraFeed");

        if (_quadRenderer != null && _visibleMat != null)
            _quadRenderer.material = _visibleMat;

        if (passthroughLayer != null)
            passthroughLayer.enabled = false;

        if (mainCamera != null)
        {
            mainCamera.clearFlags = CameraClearFlags.SolidColor;
            mainCamera.backgroundColor = Color.black;
        }

        if (_mjpeg != null)
        {
            ALog("ModeManager", "Reassigning stream texture");
            _mjpeg.ReassignTexture();
        }
    }

    static void ALog(string tag, string msg)
    {
        Debug.Log($"[{tag}] {msg}");
#if UNITY_ANDROID && !UNITY_EDITOR
        try
        {
            using var c = new AndroidJavaClass("android.util.Log");
            c.CallStatic<int>("d", tag, msg);
        }
        catch { }
#endif
    }
}