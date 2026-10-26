using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;

[System.Serializable]
public class Detection
{
    public string label;
    public int x1, y1, x2, y2;
    public float depth_m;
    public bool tracking;
    public int[] contour;  
}

[System.Serializable]
public class DetectionResponse
{
    public List<Detection> detections;
}

public class DetectionOverlay : MonoBehaviour
{
    public CameraPoseFetcher poseFetcher;
    [Header("Network")]
    public string jetsonHostname = "100.93.247.80";
    public int jetsonPort = 8080;
    public float pollInterval = 0.15f;

    [Header("Stream resolution (must match Python)")]
    public int streamWidth = 640;
    public int streamHeight = 360;

    [Header("Fixed Camera — World Geometry")]
    public Vector3 cameraWorldPosition = Vector3.zero;
    public Vector3 cameraWorldEuler = Vector3.zero;
    public float cameraHFov = 90f;
    public float minDepth = 0.3f;
    public float maxDepth = 15f;
    public float fallbackDepth = 3f;
    [Tooltip("Tick if the outline appears horizontally mirrored vs the real scene.")]
    public bool mirrorX = false;

    [Header("Calibration Trim")]
    [Tooltip("Each thumbstick step changes trim by this many degrees.")]
    public float trimStepDegrees = 1f;

    [Tooltip("How long (seconds) before the next step triggers while holding the stick.")]
    public float trimRepeatDelay = 0.2f;

    public float yawTrimDegrees = 0f;
    public float pitchTrimDegrees = 0f;

    [Header("Boot Calibration (B button)")]
    [Tooltip("Physical distance in metres from your seated head position to the camera. " +
             "Measure once with a tape measure.")]
    public float physicalDistanceToCamera = 3.5f;

    [Tooltip("Hold B for this many seconds to reset trim to zero (short press = calibrate).")]
    public float bLongPressDuration = 1.5f;

    [Header("Outline Appearance")]
    [Tooltip("Width of the person outline in world units (metres).")]
    public float outlineWidth = 0.03f;
    public Color outlineColor = new Color(0f, 1f, 0.4f, 1f);

    [Header("Depth Label (shown above outline, no prefab needed)")]
    [Tooltip("World-space height of the text in metres.")]
    public float labelSize = 0.12f;
    public Color labelColor = new Color(0f, 1f, 0.4f, 1f);

    [Header("Depth Smoothing (Unity-side EMA)")]
    [Tooltip("0 = frozen, 1 = no smoothing. 0.15 recommended for MiDaS.")]
    [Range(0.05f, 1.0f)]
    public float depthAlpha = 0.15f;

    [Header("Depth Scale")]
    [Tooltip("MiDaS depth is not metric. Scale it until the outline sits at the " +
             "right distance. Start at 1.0. Typical range 0.3 – 2.0.")]
    public float depthScale = 1.0f;

    [Header("Contour Smoothing")]
    [Tooltip("Catmull-Rom subdivisions per segment. 3 = smooth, 1 = raw points.")]
    [Range(1, 6)]
    public int splineSubdivisions = 3;

    [Header("Scene refs")]
    public Renderer videoScreen;
    public GameObject markerPrefab;

    
    private Quaternion _baseRotation;
    private Quaternion _cameraWorldRotation;

    private float _yawRepeatTimer = 0f;
    private float _pitchRepeatTimer = 0f;
    private bool _yawWasNeutral = true;
    private bool _pitchWasNeutral = true;

    private float _bHoldTimer = 0f;
    private bool _bLongPressTriggered = false;

    private Dictionary<int, ThroughWallMarker> _markerPool = new();
    private Dictionary<int, LineRenderer> _outlinePool = new();
    private Dictionary<int, TextMesh> _labelPool = new();
    private Dictionary<int, float> _depthEma = new();

    private bool _passthroughMode = false;
    private bool _lastPollFailed = false;

    private Material _outlineMaterial;

  //  for Gimbal 
    private float _gimbalYaw = 0f;
    private float _gimbalPitch = 0f;

    void Start()
    {
        _baseRotation = Quaternion.identity;
        RebuildRotation();

        _outlineMaterial = new Material(Shader.Find("UI/Default"));
        _outlineMaterial.SetInt("_ZWrite", 0);
        _outlineMaterial.SetInt("_ZTest", (int)UnityEngine.Rendering.CompareFunction.Always);
        _outlineMaterial.renderQueue = 4500;

        _passthroughMode = true;
        StartCoroutine(PollLoop());
    }

    void Update()
    {
        Vector2 stick = OVRInput.Get(OVRInput.Axis2D.SecondaryThumbstick);

        bool yawChanged = HandleAxis(stick.x, ref yawTrimDegrees,
                                       ref _yawWasNeutral, ref _yawRepeatTimer);
        bool pitchChanged = HandleAxis(stick.y, ref pitchTrimDegrees,
                                       ref _pitchWasNeutral, ref _pitchRepeatTimer);

        if (yawChanged || pitchChanged)
        {
            RebuildRotation();
            Debug.Log($"[Calib] Yaw={yawTrimDegrees:F1}°  Pitch={pitchTrimDegrees:F1}°" +
                      $"  Forward={_cameraWorldRotation * Vector3.forward}");
        }

        // b button setup
        if (OVRInput.Get(OVRInput.Button.Two))
        {
            _bHoldTimer += Time.deltaTime;

            if (_bHoldTimer >= bLongPressDuration && !_bLongPressTriggered)
            {
                _bLongPressTriggered = true;
                yawTrimDegrees = 0f;
                pitchTrimDegrees = 0f;
                RebuildRotation();
                Debug.Log("[Calib] Trim reset to 0 (long press)");
            }
        }

        if (OVRInput.GetUp(OVRInput.Button.Two))
        {
            if (!_bLongPressTriggered)
                CalibrateCamera();

            _bHoldTimer = 0f;
            _bLongPressTriggered = false;
        }
    }


    void CalibrateCamera()
    {
        if (Camera.main == null)
        {
            Debug.LogError("[Calib] Camera.main is null — calibration failed");
            return;
        }

        Transform head = Camera.main.transform;

        Vector3 headForward = head.forward;
        headForward.y = 0f;
        headForward.Normalize();

        
        cameraWorldPosition = head.position + headForward * physicalDistanceToCamera;

        cameraWorldEuler = Quaternion.LookRotation(headForward, Vector3.up).eulerAngles;

        yawTrimDegrees = 0f;
        pitchTrimDegrees = 0f;

        RebuildRotation();

        Debug.Log($"[Calib] Done!  " +
                  $"Pos={cameraWorldPosition}  " +
                  $"Euler={cameraWorldEuler}  " +
                  $"Dist={physicalDistanceToCamera}m");
    }

    bool HandleAxis(float axis, ref float trim,
                    ref bool wasNeutral, ref float repeatTimer)
    {
        const float deadzone = 0.5f;
        bool active = Mathf.Abs(axis) > deadzone;

        if (!active)
        {
            wasNeutral = true;
            repeatTimer = 0f;
            return false;
        }

        float sign = Mathf.Sign(axis);

        if (wasNeutral)
        {
            trim += sign * trimStepDegrees;
            wasNeutral = false;
            repeatTimer = 0f;
            return true;
        }

        repeatTimer += Time.deltaTime;
        if (repeatTimer >= trimRepeatDelay)
        {
            trim += sign * trimStepDegrees;
            repeatTimer = 0f;
            return true;
        }

        return false;
    }

    void RebuildRotation()
    {
        _cameraWorldRotation =
            Quaternion.Euler(cameraWorldEuler)
            * Quaternion.Euler(-pitchTrimDegrees, yawTrimDegrees, 0f);
    }

    public void SetPassthroughMode(bool passthrough)
    {
        _passthroughMode = passthrough;
        if (!_passthroughMode)
        {
            foreach (var m in _markerPool.Values)
                if (m != null) m.Hide();
            HideAllOutlines();
        }
    }

    IEnumerator PollLoop()
    {
        string url = $"http://{jetsonHostname}:{jetsonPort}/detections";
        while (true)
        {
            using var req = UnityWebRequest.Get(url);
            req.timeout = 2;
            yield return req.SendWebRequest();

            if (req.result == UnityWebRequest.Result.Success)
            {
                _lastPollFailed = false;
                var data = JsonUtility.FromJson<DetectionResponse>(req.downloadHandler.text);
                if (data?.detections != null)
                    UpdateOverlay(data.detections);
            }
            else
            {
                if (!_lastPollFailed)
                    Debug.LogWarning("[DetectionOverlay] Poll failed: " + req.error);
                _lastPollFailed = true;
                HideAll();
            }

            yield return new WaitForSeconds(pollInterval);
        }
    }

    void UpdateOverlay(List<Detection> detections)
    {
        if (_passthroughMode) UpdateMarkers(detections);
    }

    Vector3 PixelToWorldPosition(int px, int py, float rawDepth)
    {
        float depth = rawDepth * depthScale;
        if (depth < minDepth || depth > maxDepth) depth = fallbackDepth;

        float ndcX = (px / (float)streamWidth) * 2f - 1f;
        float ndcY = -(py / (float)streamHeight) * 2f + 1f;

        if (mirrorX) ndcX = -ndcX;

        float tanH = Mathf.Tan(cameraHFov * 0.5f * Mathf.Deg2Rad);
        float tanV = tanH * ((float)streamHeight / streamWidth);

        Vector3 localRay = new Vector3(ndcX * tanH, ndcY * tanV, 1f).normalized;

        Quaternion finalRot = _cameraWorldRotation
                              * Quaternion.Euler(-_gimbalPitch, _gimbalYaw, 0f);
        Vector3 basePos = cameraWorldPosition + (poseFetcher != null && poseFetcher.poseValid ? poseFetcher.cameraPoseOffset : Vector3.zero);
        return basePos + finalRot * localRay * depth;
    }

    Vector3 DetectionToWorldPosition(Detection det)
    {
        float cx = (det.x1 + det.x2) * 0.5f;
        float cy = (det.y1 + det.y2) * 0.5f;
        return PixelToWorldPosition((int)cx, (int)cy, det.depth_m);
    }

    float SmoothedDepth(int idx, float rawDepth)
    {
        if (rawDepth <= minDepth || rawDepth >= maxDepth)
            return _depthEma.ContainsKey(idx) ? _depthEma[idx] : fallbackDepth;

        if (!_depthEma.ContainsKey(idx))
        {
            _depthEma[idx] = rawDepth;
            return rawDepth;
        }

        _depthEma[idx] = depthAlpha * rawDepth + (1f - depthAlpha) * _depthEma[idx];
        return _depthEma[idx];
    }

    Vector3[] CatmullRomSpline(Vector3[] pts, int subdivisions)
    {
        if (pts.Length < 3) return pts;
        int n = pts.Length;
        var result = new List<Vector3>(n * subdivisions);

        for (int i = 0; i < n; i++)
        {
            Vector3 p0 = pts[(i - 1 + n) % n];
            Vector3 p1 = pts[i];
            Vector3 p2 = pts[(i + 1) % n];
            Vector3 p3 = pts[(i + 2) % n];

            for (int s = 0; s < subdivisions; s++)
            {
                float t = s / (float)subdivisions;
                float t2 = t * t;
                float t3 = t2 * t;
                result.Add(0.5f * (
                      2f * p1
                    + (-p0 + p2) * t
                    + (2f * p0 - 5f * p1 + 4f * p2 - p3) * t2
                    + (-p0 + 3f * p1 - 3f * p2 + p3) * t3
                ));
            }
        }
        return result.ToArray();
    }

    void UpdateMarkers(List<Detection> detections)
    {
        HashSet<int> activeIndices = new HashSet<int>();

        for (int i = 0; i < detections.Count; i++)
        {
            Detection det = detections[i];
            activeIndices.Add(i);

            float smoothDepth = SmoothedDepth(i, det.depth_m);
            det.depth_m = smoothDepth;
            Vector3 personWorldPos = DetectionToWorldPosition(det);
            float displayDepth = Vector3.Distance(Camera.main.transform.position, personWorldPos);

            if (markerPrefab != null)
            {
                if (!_markerPool.ContainsKey(i) || _markerPool[i] == null)
                {
                    var go = Instantiate(markerPrefab, transform);
                    _markerPool[i] = go.GetComponent<ThroughWallMarker>();
                    if (_markerPool[i] == null) Destroy(go);
                }

                if (_markerPool.ContainsKey(i) && _markerPool[i] != null)
                {
                    Vector3 worldPos = DetectionToWorldPosition(det);
                    _markerPool[i].SetData(det.label.Split(' ')[0], displayDepth, worldPos);
                }
            }

            UpdateOutline(i, det, displayDepth);
        }

        foreach (var kvp in _markerPool)
            if (!activeIndices.Contains(kvp.Key) && kvp.Value != null)
                kvp.Value.Hide();

        foreach (var kvp in _outlinePool)
            if (!activeIndices.Contains(kvp.Key) && kvp.Value != null)
                kvp.Value.positionCount = 0;

        foreach (var kvp in _labelPool)
            if (!activeIndices.Contains(kvp.Key) && kvp.Value != null)
                kvp.Value.gameObject.SetActive(false);

        List<int> staleEma = new List<int>();
        foreach (var k in _depthEma.Keys)
            if (!activeIndices.Contains(k)) staleEma.Add(k);
        foreach (var k in staleEma) _depthEma.Remove(k);
    }

    void UpdateOutline(int idx, Detection det, float displayDepth)
    {
        if (!_outlinePool.ContainsKey(idx) || _outlinePool[idx] == null)
        {
            var go = new GameObject($"PersonOutline_{idx}");
            go.transform.SetParent(transform);

            var lr = go.AddComponent<LineRenderer>();
            lr.useWorldSpace = true;
            lr.loop = true;
            lr.startWidth = outlineWidth;
            lr.endWidth = outlineWidth;
            lr.material = _outlineMaterial;
            lr.startColor = outlineColor;
            lr.endColor = outlineColor;
            lr.numCapVertices = 8;
            lr.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            lr.receiveShadows = false;

            _outlinePool[idx] = lr;
        }

        LineRenderer line = _outlinePool[idx];
        line.enabled = true;

        if (det.contour == null || det.contour.Length < 4)
        {
            DrawBoundingBoxOutline(line, det);
            UpdateDepthLabel(idx, det, displayDepth);
            return;
        }

        int pointCount = det.contour.Length / 2;
        if (pointCount < 3)
        {
            DrawBoundingBoxOutline(line, det);
            UpdateDepthLabel(idx, det, displayDepth);
            return;
        }

        var raw = new Vector3[pointCount];
        for (int p = 0; p < pointCount; p++)
            raw[p] = PixelToWorldPosition(
                det.contour[p * 2], det.contour[p * 2 + 1], det.depth_m);

        Vector3[] smoothed = CatmullRomSpline(raw, splineSubdivisions);

        line.positionCount = smoothed.Length;
        for (int p = 0; p < smoothed.Length; p++)
            line.SetPosition(p, smoothed[p]);

        UpdateDepthLabel(idx, det, displayDepth);
    }

    void UpdateDepthLabel(int idx, Detection det, float displayDepth)
    {
        if (!_labelPool.ContainsKey(idx) || _labelPool[idx] == null)
        {
            var go = new GameObject($"DepthLabel_{idx}");
            go.transform.SetParent(transform);

            var tm = go.AddComponent<TextMesh>();
            tm.anchor = TextAnchor.LowerCenter;
            tm.alignment = TextAlignment.Center;
            tm.fontSize = 48;
            tm.color = labelColor;

            var mr = go.GetComponent<MeshRenderer>();
            mr.material.renderQueue = 4500;

            _labelPool[idx] = tm;
        }

        TextMesh label = _labelPool[idx];
        label.gameObject.SetActive(true);

        float scaledDepth = det.depth_m * depthScale;
        string depthStr = (scaledDepth > minDepth && scaledDepth < maxDepth)
                          ? $"{displayDepth:F1} m"
                          : "-- m";
        label.text = $"person\n{depthStr}";
        label.color = labelColor;

        int topCX = (det.x1 + det.x2) / 2;
        Vector3 topWorld = PixelToWorldPosition(topCX, det.y1, det.depth_m);
        label.transform.position = topWorld + Vector3.up * (labelSize * 0.5f);

        float scale = labelSize / 10f;
        label.transform.localScale = Vector3.one * scale;

        label.transform.LookAt(Camera.main.transform.position);
        label.transform.Rotate(0f, 180f, 0f);
    }

    void DrawBoundingBoxOutline(LineRenderer line, Detection det)
    {
        line.positionCount = 4;
        line.SetPosition(0, PixelToWorldPosition(det.x1, det.y1, det.depth_m));
        line.SetPosition(1, PixelToWorldPosition(det.x2, det.y1, det.depth_m));
        line.SetPosition(2, PixelToWorldPosition(det.x2, det.y2, det.depth_m));
        line.SetPosition(3, PixelToWorldPosition(det.x1, det.y2, det.depth_m));
    }

    void HideAllOutlines()
    {
        foreach (var lr in _outlinePool.Values)
            if (lr != null) lr.positionCount = 0;
        foreach (var tm in _labelPool.Values)
            if (tm != null) tm.gameObject.SetActive(false);
    }

    void HideAll()
    {
        foreach (var m in _markerPool.Values)
            if (m != null) m.Hide();
        HideAllOutlines();
    }


    public void SetGimbalAngles(float yaw, float pitch)
    {
        _gimbalYaw = yaw;
        _gimbalPitch = pitch;
    }

    void OnDrawGizmos()
    {
        Gizmos.color = Color.yellow;
        Gizmos.DrawWireCube(cameraWorldPosition, Vector3.one * 0.1f);
        Quaternion rot = Quaternion.Euler(cameraWorldEuler);
        Gizmos.color = Color.cyan;
        Gizmos.DrawLine(cameraWorldPosition,
                        cameraWorldPosition + rot * Vector3.forward * 2f);
    }
}