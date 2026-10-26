using UnityEngine;

public class HeadTracker : MonoBehaviour
{
    void Update()
    {
        Transform headset = Camera.main.transform;

        Vector3 forward = headset.forward;

        float yaw = Mathf.Atan2(forward.x, forward.z) * Mathf.Rad2Deg;
        float pitch = Mathf.Asin(forward.y) * Mathf.Rad2Deg;

        Debug.Log("Yaw: " + yaw + " Pitch: " + pitch);
    }
}