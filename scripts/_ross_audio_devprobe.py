import pyaudiowpatch as pa

p = pa.PyAudio()
try:
    wi = p.get_host_api_info_by_type(pa.paWASAPI)
    print("WASAPI defaultOutputDevice index:", wi["defaultOutputDevice"])
    dod = p.get_device_info_by_index(wi["defaultOutputDevice"])
    print("WASAPI default output name:", dod["name"])
except Exception as e:
    print("wasapi info err:", e)
print("--- loopback devices ---")
for d in p.get_loopback_device_info_generator():
    print(d["index"], "|", d["name"], "| rate", int(d["defaultSampleRate"]), "| inCh", d["maxInputChannels"])
p.terminate()
