from filelock import FileLock
from pocketbase import PocketBase
from pocketbase.client import FileUpload
import os
import urllib.request
import urllib.parse
import subprocess

pb = PocketBase(os.environ["PB_ADDR"])
pb_user = pb.collection("upscale_runners").auth_with_password(os.environ["PB_USER"],os.environ["PB_PASSWORD"])

PB_COLLECTION_IMAGE = "generated_images"
PB_COLLECTION_SETTINGS = "settings"

do_upscale = True

def upscale():
    global do_upscale
    
    try:
        to_upscale_record = pb.collection(PB_COLLECTION_IMAGE).get_first_list_item("selected=true && upscaled=false && runner=''",{
            "sort": "@random",
            'expand': 'queue'
        })
    except:
        # if no record found, will throw exception, and exit
        print("Found 0 images to upscale, quitting")
        do_upscale = False
        return

    # lock the runner, if the lock failed, will throw exception, and run this function again
    pb.collection(PB_COLLECTION_IMAGE).update(to_upscale_record.id,body_params={
        "runner": pb_user.record.id
    })
    
    settings_record = pb.collection(PB_COLLECTION_SETTINGS).get_first_list_item('')
    queue_record = to_upscale_record.expand['queue']
    upscale_times = queue_record.upscale_times
    if queue_record.upscale_times==0:
        upscale_times = settings_record.upscale_times # type: ignore
    
    file_url = '{pb_host}/api/files/generated_images/{id}/{file}'.format(pb_host=pb.base_url,id=to_upscale_record.id,file=to_upscale_record.image)
    origin_file = 'to-upscale.png'
    upscaled_file_name = '{}.png'.format(to_upscale_record.id)
    urllib.request.urlretrieve(urllib.parse.urlparse(file_url).geturl(), origin_file)
    try:
        result = subprocess.run([
            "./realesrgan-ncnn-vulkan", 
            "-s", str(upscale_times),
            "-n", settings_record.upscale_model,
            "-i", origin_file, 
            "-o", upscaled_file_name
        ], check=True)
        if result.returncode != 0:
            raise ChildProcessError("Upscale failed.")
        if os.path.getsize(upscaled_file_name) == os.path.getsize(origin_file):
            raise ChildProcessError("Upscale failed.")
    except Exception as e:
        # unlock the runner
        pb.collection(PB_COLLECTION_IMAGE).update(to_upscale_record.id,body_params={
            "runner": ""
        })
        print(e)
        return
        
    pb.collection(PB_COLLECTION_IMAGE).update(to_upscale_record.id,{
        'image':''
    })
    pb.collection(PB_COLLECTION_IMAGE).update(to_upscale_record.id,{
        'image':FileUpload(upscaled_file_name,open(upscaled_file_name,'rb')),
        'upscaled':True
    })
    os.remove(origin_file)
    os.remove(upscaled_file_name)
    print("Upscale image [{}] finished".format(to_upscale_record.id))
    # by not setting do_upscale=False at the function end, the loop in main will execute upscale function again
    # do_upscale=False

if __name__ == '__main__':
    lock = FileLock('/var/lock/run.lock')
    try:
        lock.acquire(blocking=True)
    except:
        print("Another upscale process is running.")
        exit()

    while do_upscale:
        try:
            upscale()
        except:
            pass
    
    lock.release()
    