using System.Collections;
using UnityEngine;
using UnityEngine.Networking;

[System.Serializable]
public class CameraPoseData
{
    public float x;
    public float y;
    public float z;
    public bool ok;
}

public class CameraPoseFetcher : MonoBehaviour
{
    [Header("Network")]
    public string jetsonHostname = "100.93.247.80";
    public int jetsonPort = 8080;
    public float pollInterval = 0.1f;

    [Header("Output")]
    public Vector3 cameraPoseOffset = Vector3.zero;
    public bool poseValid = false;

    void Start() => StartCoroutine(PollLoop());

    IEnumerator PollLoop()
    {
        string url = $"http://{jetsonHostname}:{jetsonPort}/camera_pose";
        while (true)
        {
            using var req = UnityWebRequest.Get(url);
            req.timeout = 2;
            yield return req.SendWebRequest();

            if (req.result == UnityWebRequest.Result.Success)
            {
                var data = JsonUtility.FromJson<CameraPoseData>(req.downloadHandler.text);
                poseValid = data != null && data.ok;
                if (poseValid)
                {
                    Vector3 newPose = new Vector3(data.x, data.y, data.z);

                    if ((newPose - cameraPoseOffset).sqrMagnitude > 0.000001f)
                    {
                        cameraPoseOffset = newPose;

                        Debug.Log(
                            $"[VO] ΔX={cameraPoseOffset.x:F3}  " +
                            $"ΔY={cameraPoseOffset.y:F3}  " +
                            $"ΔZ={cameraPoseOffset.z:F3}"
                        );
                    }
                }
            }
            yield return new WaitForSeconds(pollInterval);
        }
    }
}