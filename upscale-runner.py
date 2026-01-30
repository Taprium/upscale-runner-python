import asyncio
import socket
import time
from pocketbase import PocketBase
from pocketbase.client import FileUpload # type: ignore
from pocketbase.services.realtime_service import MessageData
import os
import urllib.request
import subprocess
import schedule
import requests

pb = PocketBase(os.environ["TAPRIUIM_ADDR"])
PB_SECRET=os.environ["TAPRIUM_AUTH_SECRET"]

PB_COLLECTION_IMAGE = "generated_images"
PB_COLLECTION_SETTINGS = "settings"
PB_COLLECTION_IMAGE_QUEUES = 'image_queues'
PB_COLLECTION_UPSCALE_RUNNERS = "upscale_runners"

last_heartbeat = time.time()

def try_sign_in(max_attempts=100):
    """
    Attempts to authenticate. If the device is pending verification (202),
    it blocks and retries. Returns the JWT token on success.
    """
    def _get_machine_id():
        """Reads the unique hardware ID from the Linux host."""
        try:
            with open('/etc/machine-id', 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            # Fallback to hostname if machine-id is inaccessible
            return socket.gethostname()
        
    machine_id = _get_machine_id()
    hostname = socket.gethostname()
    url = f"{pb.base_url.rstrip('/')}/api/cluster/auth"
    
    payload = {
        "secret": PB_SECRET,
        "machine_id": machine_id,
        "hostname": hostname
    }

    attempts = 0
    while attempts < max_attempts:
        try:
            response = requests.post(url, json=payload, timeout=10)
            
            # 200 OK: Access Granted
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Access Granted for {hostname}")
                return data.get("token")

            # 202 Accepted or 403 Forbidden: Pending Verification
            elif response.status_code in [202, 403]:
                print(f"⏳ Waiting for admin to verify {hostname} ({machine_id})...")
            
            else:
                print(f"❌ Auth failed with status: {response.status_code}")
                response.raise_for_status()

        except requests.exceptions.RequestException as e:
            print(f"⚠️ Connection error: {e}. Retrying...")

        attempts += 1
        time.sleep(10)  # Wait 10 seconds before checking again
    
    raise Exception("Maximum authentication attempts reached.")

def upscale_single(to_upscale_record):
    # lock the runner, if the lock failed, will throw exception
    image_record = pb.collection(PB_COLLECTION_IMAGE).update(to_upscale_record.id,body_params={
        "runner": pb.auth_store.base_model.id # type: ignore
    },query_params={'expand':'queue'})
    
    settings_record = pb.collection(PB_COLLECTION_SETTINGS).get_first_list_item('')

    queue_record = image_record.expand['queue']

    upscale_times = queue_record.upscale_times # type: ignore
    if upscale_times == 0:
        upscale_times = settings_record.upscale_times # type: ignore
    
    token = pb.files.get_token()
    
    file_url = pb.files.get_url(image_record,image_record.image,{'token':token}) # type: ignore
    origin_file = image_record.image # type: ignore
    upscaled_file_name = 'upscaled-{}'.format(origin_file)
    urllib.request.urlretrieve(file_url, origin_file)

    command = [
        "./realesrgan-ncnn-vulkan", 
        "-s", str(upscale_times),
        "-n", settings_record.upscale_model, # type: ignore
        "-i", origin_file, 
        "-o", upscaled_file_name
    ]
    print(f"Executing command: {command}")
    try:
        result = subprocess.run(command, check=True)
        if result.returncode != 0:
            raise ChildProcessError("Upscale failed.")
        if os.path.getsize(upscaled_file_name) == os.path.getsize(origin_file):
            raise ChildProcessError("Upscale failed.")
    except Exception as e:
        # unlock the runner
        pb.collection(PB_COLLECTION_IMAGE).update(image_record.id,body_params={
            "runner": ""
        })
        print(e)
        return
        
    # pb.collection(PB_COLLECTION_IMAGE).update(image_record.id,{
    #     'image':''
    # })
    pb.collection(PB_COLLECTION_IMAGE).update(image_record.id,{
        'image':FileUpload(upscaled_file_name,open(upscaled_file_name,'rb')),
        'upscaled':True
    })
    os.remove(origin_file)
    os.remove(upscaled_file_name)
    print("Upscale image [{}] finished".format(image_record.id))
    pass

def left_over_check():
    global last_heartbeat
    do_upscale = True
    while do_upscale:
        try:
            to_upscale_record = pb.collection(PB_COLLECTION_IMAGE).get_first_list_item(
                f"selected=true && upscaled=false && (runner='' || runner='{pb.auth_store.base_model.id}')",  # type: ignore
                {
                    "sort": "@random",
                    'expand': 'queue'
                })
        except:
            # if no record found, will throw exception, and exit
            print("Found 0 images to upscale, quitting")
            do_upscale = False
            return

        last_heartbeat = time.time()
        upscale_single(to_upscale_record)

def pb_refresh_auth():
    pb.collection(PB_COLLECTION_UPSCALE_RUNNERS).auth_refresh()

def server_heartbeat_hook(data:MessageData):
    """Triggered by the Go Cron updating 'last_seen'"""
    global last_heartbeat
    if data.record.id == pb.auth_store.base_model.id: # type: ignore
        last_heartbeat = time.time()

def new_image_upscale_hook(data:MessageData):
    """Triggered by your actual business logic"""
    global last_heartbeat
    last_heartbeat = time.time()
    r = data.record
    if data.action=='update' and r.selected==True and r.upscaled==False and r.runner=='': # type: ignore
        print("New image to upscale.")
        upscale_single(r)

async def main_monitor_loop():
    global last_heartbeat
    
    # Keep track of our subscription state
    while True:
        try:
            print("📡 Attempting to connect to PocketBase Realtime...")
            
            # 1. Refresh Auth (Best to do this before every new subscription)
            token = try_sign_in()
            pb.auth_store.save(token)
            pb.collection(PB_COLLECTION_UPSCALE_RUNNERS).auth_refresh()
            print(f"✅ Authenticated as node: {pb.auth_store.base_model.id}") # type: ignore
            last_heartbeat = time.time()

            await asyncio.sleep(1) # Give the connection a heartbeat to stabilize

            # 2. Daily Auth Refresh Schedule
            schedule.clear() # Clear old jobs to prevent duplicates on reconnect
            schedule.every().day.at("00:00").do(pb_refresh_auth)
            
            # 4. Subscribe
            # Note: We assign the result to a variable so we can clean up later
            pb.realtime.subscribe(PB_COLLECTION_UPSCALE_RUNNERS, server_heartbeat_hook) # type: ignore
            print(f"📡 Subscribed to {PB_COLLECTION_UPSCALE_RUNNERS}. Waiting for events...")
            pb.realtime.subscribe(PB_COLLECTION_IMAGE, new_image_upscale_hook)
            print(f"📡 Subscribed to {PB_COLLECTION_IMAGE}. Waiting for events...")

            # 3. Run initial checks
            left_over_check()
            print("🚀 Startup left-over check finished.")

            # 5. The Heartbeat/Monitor Loop
            while True:
                # Check for "Zombie" state
                # If Go Cron is 5 mins, we wait 7 mins (420s) before panicking
                if time.time() - last_heartbeat > 420:
                    raise ConnectionError("No heartbeat or work detected for 7 minutes.")

                schedule.run_pending()
                # Check if we are still connected (simplified check)
                # Some SDK versions have pb.realtime.is_connected
                await asyncio.sleep(60) 

        except Exception as e:
            print(f"⚠️ Connection lost or Error: {e}")
            print("🔄 Retrying in 10 seconds...")
            
            # Clean up old subscription before retrying
            try:
                pb.realtime.unsubscribe()
            except:
                pass
                
            await asyncio.sleep(10) # Wait before the next 'while' iteration
            continue # Jump to the top of the loop to try again

        except asyncio.CancelledError:
            print("🛑 Monitoring cancelled.")
            break
        except KeyboardInterrupt:
            print("\n🛑 Manual shutdown.")
            break

    # Final cleanup on total exit
    # pb.collection(PB_COLLECTION_NOCO_SOURCES).unsubscribe()
    pb.realtime.unsubscribe()
    print("✅ Cleanup complete.")

if __name__ == '__main__':
    # We moved the login logic INSIDE the main_monitor_loop's retry logic
    # so that it can handle password changes or server reboots automatically.
    try:
        asyncio.run(main_monitor_loop())
    except KeyboardInterrupt:
        print("\n🛑 Script stopped by user.")

    