import sounddevice as sd

def _probe_input_device(index, samplerate=48000, blocksize=240):
    try:
        info = sd.query_devices(index)
        if int(info.get("max_input_channels", 0)) <= 0:
            return False
        ch = 1 if info["max_input_channels"] >= 1 else info["max_input_channels"]
        stream = sd.InputStream(device=index, channels=ch or 1, samplerate=samplerate, blocksize=blocksize, dtype="float32")
        stream.start(); stream.stop(); stream.close()
        return True
    except Exception:
        return False

def _probe_output_device(index, samplerate=48000, blocksize=240):
    try:
        info = sd.query_devices(index)
        if int(info.get("max_output_channels", 0)) <= 0:
            return False
        ch = 1 if info["max_output_channels"] >= 1 else info["max_output_channels"]
        stream = sd.OutputStream(device=index, channels=ch or 1, samplerate=samplerate, blocksize=blocksize, dtype="float32")
        stream.start(); stream.stop(); stream.close()
        return True
    except Exception:
        return False

def scan_filtered_devices():
    devices = sd.query_devices()
    inputs, outputs = [], []
    for i, dev in enumerate(devices):
        name = dev.get("name", f"Device {i}")
        label = f"{i}: {name}"
        if int(dev.get("max_input_channels", 0)) > 0 and _probe_input_device(i):
            inputs.append((i, label))
        if int(dev.get("max_output_channels", 0)) > 0 and _probe_output_device(i):
            outputs.append((i, label))
    return inputs, outputs
