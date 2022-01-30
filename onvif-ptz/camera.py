#!/usr/bin/env python3



import argparse
import threading
import json
import sys
import os
import calendar
from datetime import datetime, timedelta
import signal
import random
import time
import re
import requests
from requests.auth import HTTPDigestAuth
import errno
import paho.mqtt.client as mqtt 
from json.decoder import JSONDecodeError
from sensecam_control import onvif_control
import utils
import logging
import coloredlogs

logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.storage.common.storageclient").setLevel(logging.WARNING)

from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient, __version__

ID = str(random.randint(1,100001))
args = None
camera = None
cameraBearingCorrection = 0
cameraElevationCorrection = 0
cameraConfig = None
cameraZoom = None
cameraMoveSpeed = None
cameraDelay = None
cameraLead = 0 
active = False
blob_service_client = None 


object_topic = None
flight_topic = None
config_topic = "skyscan/config/json"

bearing = 0         # this is an angle
elevation = 0       # this is an angle
cameraPan = 0       # This value is in angles 
cameraTilt = 0      # This values is in angles 
distance3d = 0      # this is in Meters
distance2d = 0      # in meters
angularVelocityHorizontal = 0      # in meters
angularVelocityVertical = 0      # in meters
planeTrack = 0      # This is the direction that the plane is moving in

currentPlane=None
trackId = None


def calculate_bearing_correction(b):
    return (b + cameraBearingCorrection) % 360

def calculate_elevation_correction(e):
    corrected = e + cameraElevationCorrection
    if corrected > 90:
        corrected = 90
    return corrected
         
def get_jpeg_request():  # 5.2.4.1
    """
    The requests specified in the JPEG/MJPG section are supported by those video products
    that use JPEG and MJPG encoding.
    Args:
        resolution: Resolution of the returned image. Check the product’s Release notes.
        camera: Selects the source camera or the quad stream.
        square_pixel: Enable/disable square pixel correction. Applies only to video encoders.
        compression: Adjusts the compression level of the image.
        clock: Shows/hides the time stamp. (0 = hide, 1 = show)
        date: Shows/hides the date. (0 = hide, 1 = show)
        text: Shows/hides the text. (0 = hide, 1 = show)
        text_string: The text shown in the image, the string must be URL encoded.
        text_color: The color of the text shown in the image. (black, white)
        text_background_color: The color of the text background shown in the image.
        (black, white, transparent, semitransparent)
        rotation: Rotate the image clockwise.
        text_position: The position of the string shown in the image. (top, bottom)
        overlay_image: Enable/disable overlay image.(0 = disable, 1 = enable)
        overlay_position:The x and y coordinates defining the position of the overlay image.
        (<int>x<int>)
    Returns:
        Success ('image save' and save the image in the file folder) or Failure (Error and
        description).
    """
    payload = {
        'resolution': "1920x1080",
        'compression': 5,
        'camera': 1,
    }
    url = 'http://' + args.axis_ip + '/jpegpull/snapshot'
    start_time = datetime.now()
    try:
        resp = requests.get(url, auth=HTTPDigestAuth(args.axis_username, args.axis_password), params=payload, timeout=0.5)
    except requests.exceptions.Timeout:
        logging.error("🚨 Images capture request timed out 🚨  ")
        return
    except:
        logging.exception("🚨 Images capture request timed out 🚨  ")
        return        
    callsign = currentPlane["callsign"]
    if len(callsign) <= 0:
        callsign=""
    disk_time = datetime.now()
    if resp.status_code == 200:
        captureDir = "capture/{}".format(currentPlane["type"])
        try:
            os.makedirs(captureDir)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise  # This was not a "directory exist" error..
        filename = "{}_{}_{}_{}_{}_{}_{}.jpg".format(currentPlane["icao24"], int(bearing), int(elevation), int(distance3d), datetime.now().strftime('%Y-%m-%d-%H-%M-%S'),trackId,callsign)
        filepath = "{}/{}".format(captureDir,filename)
        # Original
        with open(filepath, 'wb') as var:
            var.write(resp.content)


        if blob_service_client:
            # Create a blob client using the local file name as the name for the blob
            blob_client = blob_service_client.get_blob_client(container="inbox", blob=filename)

            # Upload the created file
            try:
                with open(filepath, "rb") as data:
                    blob_client.upload_blob(data,overwrite=True)
            except:
                logging.exception(" 🚨 Exception while Uploading")


        #Non-Blocking
        #fd = os.open(filename, os.O_CREAT | os.O_WRONLY | os.O_NONBLOCK)
        #os.write(fd, resp.content)
        #os.close(fd)

        # Blocking

        #fd = os.open(filename, os.O_CREAT | os.O_WRONLY)
        #os.write(fd, resp.content)
        #os.close(fd)
    else:
        logging.error("Unable to fetch image: {}\tstatus: {}".format(url,resp.status_code))

    end_time = datetime.now()
    net_time_diff = (disk_time - start_time)
    disk_time_diff = (end_time - disk_time)
    if disk_time_diff.total_seconds() > 0.1:
        logging.warning("🚨  Image Capture Timeout  🚨  Net time: {}  \tDisk time: {}".format(net_time_diff, disk_time_diff))



def calculateCameraPosition():
    global cameraPan
    global cameraTilt
    global distance2d
    global distance3d
    global bearing
    global angularVelocityHorizontal
    global angularVelocityVertical
    global elevation

    (lat, lon, alt) = utils.calc_travel_3d(currentPlane, camera_lead)
    distance3d = utils.coordinate_distance_3d(camera_latitude, camera_longitude, camera_altitude, lat, lon, alt)
    #(latorig, lonorig) = utils.calc_travel(observation.getLat(), observation.getLon(), observation.getLatLonTime(),  observation.getGroundSpeed(), observation.getTrack(), camera_lead)
    distance2d = utils.coordinate_distance(camera_latitude, camera_longitude, lat, lon)
    bearing = utils.bearingFromCoordinate( cameraPosition=[camera_latitude, camera_longitude], airplanePosition=[lat, lon], heading=currentPlane["track"])
    elevation = utils.elevation(distance2d, cameraAltitude=camera_altitude, airplaneAltitude=alt)
    (angularVelocityHorizontal, angularVelocityVertical) = utils.angular_velocity(currentPlane,camera_latitude, camera_longitude, camera_altitude) 
    #logging.info("Angular Velocity - Horizontal: {} Vertical: {}".format(angularVelocityHorizontal, angularVelocityVertical))
    cameraTilt = calculate_elevation_correction(elevation)
    cameraPan = utils.cameraPanFromCoordinate(cameraPosition=[camera_latitude, camera_longitude], airplanePosition=[lat, lon])
    cameraPan = calculate_bearing_correction(cameraPan)



def moveCamera(ip, username, password):

    movePeriod = 1000  # milliseconds
    capturePeriod = 1000 # milliseconds
    moveTimeout = datetime.now()
    captureTimeout = datetime.now()
    camera = onvif_control.CameraControl(ip, username, password)
    camera.camera_start()


    while True:
        if active:
            if not "icao24" in currentPlane:
                logging.warning(" 🚨 Active but Current Plane is not set")
                continue
            if moveTimeout <= datetime.now():
                calculateCameraPosition()

                if cameraPan > 180:
                    pan = (cameraPan%180)-180
                else:
                    pan = cameraPan
                pan = pan / 180

                tilt = cameraTilt / 90

                try:
                    camera.absolute_move(pan, tilt, cameraZoom)
                except Exception as exc:
                    logging.error(" 🚨 Exception with Moving: {}".format(exc))   
                  
                #logging.info("Moving to Pan: {} Tilt: {}".format(cameraPan, cameraTilt))
                moveTimeout = moveTimeout + timedelta(milliseconds=movePeriod)
                if moveTimeout <= datetime.now():
                    lag = datetime.now() - moveTimeout
                    logging.warning(" 🚨 Move execution time was greater that Move Period - lag: {}".format(lag))
                    moveTimeout = datetime.now() + timedelta(milliseconds=movePeriod)

            if captureTimeout <= datetime.now():
                time.sleep(cameraDelay)
                get_jpeg_request()
                captureTimeout = captureTimeout + timedelta(milliseconds=capturePeriod)
                if captureTimeout <= datetime.now():
                    lag = datetime.now() - captureTimeout
                    logging.warning(" 🚨 Capture execution time was greater that Capture Period - lag: {}".format(lag))
                    captureTimeout = datetime.now() + timedelta(milliseconds=capturePeriod)
            time.sleep(0.005)
        else:
            time.sleep(1)

def update_track_id(icao24):
    global trackId
    now = datetime.now()
    timestamp = int(datetime.timestamp(now))
    trackId = "{}-{}".format(icao24,timestamp)
    logging.info("Setting Track ID to: {}".format(trackId))

def update_config(config):
    global cameraZoom
    global cameraMoveSpeed
    global cameraDelay
    global cameraPan
    global camera_lead
    global camera_altitude
    global cameraBearingCorrection
    global cameraElevationCorrection

    if "cameraZoom" in config:
        cameraZoom = float(config["cameraZoom"])
        logging.info("Setting Camera Zoom to: {}".format(cameraZoom))
    if "cameraDelay" in config:
        cameraDelay = float(config["cameraDelay"])
        logging.info("Setting Camera Delay to: {}".format(cameraDelay))
    if "cameraMoveSpeed" in config:
        cameraMoveSpeed = int(config["cameraMoveSpeed"])
        logging.info("Setting Camera Move Speed to: {}".format(cameraMoveSpeed))
    if "cameraLead" in config:
        camera_lead = float(config["cameraLead"])
        logging.info("Setting Camera Lead to: {}".format(camera_lead))
    if "cameraAltitude" in config:
        camera_altitude = float(config["cameraAltitude"])
        logging.info("Setting Camera Altitude to: {}".format(camera_altitude))
    if "cameraBearingCorrection" in config:
        cameraBearingCorrection = float(config["cameraBearingCorrection"])
        logging.info("Setting Camera Bearing Correction to: {}".format(cameraBearingCorrection))
    if "cameraElevationCorrection" in config:
        cameraElevationCorrection = float(config["cameraElevationCorrection"])
        logging.info("Setting Camera Elevation Correction to: {}".format(cameraElevationCorrection))          

#############################################
##         MQTT Callback Function          ##
#############################################
def on_message(client, userdata, message):
    global currentPlane
    global object_timeout
    global camera_longitude
    global camera_latitude
    global camera_altitude

    global active
 

    command = str(message.payload.decode("utf-8"))
    #rint(command)
    try:
        update = json.loads(command)
        #payload = json.loads(messsage.payload) # you can use json.loads to convert string to json
    except JSONDecodeError as e:
    # do whatever you want
        print(e)
    except TypeError as e:
    # do whatever you want in this case
        print(e)
    except ValueError as e:
        print(e)
    except:
        print("Caught it!")
    
    if message.topic == object_topic:
        logging.info("Got Object Topic")
        setXY(update["x"], update["y"])
        object_timeout = time.mktime(time.gmtime()) + 5
    elif message.topic == flight_topic:
        if "icao24" in update:
            if (currentPlane == None) or (currentPlane["icao24"] != update["icao24"]):
                update_track_id(update["icao24"])
            if active is False:
                logging.info("{}\t[Starting Capture]".format(update["icao24"]))
            else:
                logging.info("Current ICAO24: {} Updated: {}".format(currentPlane["icao24"], update["icao24"]))


            logging.info("{}\t[IMAGE]\tBearing: {} \tElv: {} \tDist: {}".format(update["icao24"],int(update["bearing"]),int(update["elevation"]),int(update["distance"])))
            currentPlane = update
            active = True

        else:
            if active is True:
                logging.info("{}\t[Stopping Capture]".format(currentPlane["icao24"]))
            active = False
            # It is better to just have the old values for currentPlane in case a message comes in while the 
            # moveCamera Thread is running.
            #currentPlane = {}        
    elif message.topic == config_topic:
        update_config(update)
        logging.info("Config Message: {}".format(update))
    elif message.topic == "skyscan/egi":
        #logging.info(update)
        camera_longitude = float(update["long"])
        camera_latitude = float(update["lat"])
        camera_altitude = float(update["alt"])
    else:
        logging.info("Message: {} Object: {} Flight: {}".format(message.topic, object_topic, flight_topic))

def main():
    global args
    global logging
    global camera
    global cameraDelay
    global cameraMoveSpeed
    global cameraBearingCorrection
    global cameraElevationCorrection
    global cameraZoom
    global cameraPan
    global camera_altitude
    global camera_latitude
    global camera_longitude
    global camera_lead
    global cameraConfig
    global flight_topic
    global object_topic
    global blob_service_client

    parser = argparse.ArgumentParser(description='An MQTT based camera controller')
    parser.add_argument('--lat', type=float, help="Latitude of camera")
    parser.add_argument('--lon', type=float, help="Longitude of camera")
    parser.add_argument('--alt', type=float, help="altitude of camera in METERS!", default=0)
    parser.add_argument('--camera-lead', type=float, help="how many seconds ahead of a plane's predicted location should the camera be positioned", default=0.1)

    parser.add_argument('-m', '--mqtt-host', help="MQTT broker hostname", default='127.0.0.1')
    parser.add_argument('-t', '--mqtt-flight-topic', help="MQTT topic to subscribe to", default="skyscan/flight/json")
    parser.add_argument( '--mqtt-object-topic', help="MQTT topic to subscribe to", default="skyscan/object/json")
    parser.add_argument('-u', '--axis-username', help="Username for the Axis camera", required=True)
    parser.add_argument('-p', '--axis-password', help="Password for the Axis camera", required=True)
    parser.add_argument('-a', '--axis-ip', help="IP address for the Axis camera", required=True)
    parser.add_argument('-s', '--camera-move-speed', type=int, help="The speed at which the Axis will move for Pan/Tilt (0-100)", default=50)
    parser.add_argument('-d', '--camera-delay', type=float, help="How many seconds after issuing a Pan/Tilt command should a picture be taken", default=0)
    parser.add_argument('-z', '--camera-zoom', type=float, help="The zoom setting for the camera (0.0 - 1.0)", default=0.3)
    parser.add_argument('-v', '--verbose',  action="store_true", help="Verbose output")
    parser.add_argument('-b', '--camera-bearing-correction', type=float, help="The amount to correct the bearing by", default=0)
    parser.add_argument('-e', '--camera-elevation-correction', type=float, help="The amount to correct camera elevation by", default=0)
    parser.add_argument("--conn", help="Azure Blob Storage connection string")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    
    styles = {'critical': {'bold': True, 'color': 'red'}, 'debug': {'color': 'green'}, 'error': {'color': 'red'}, 'info': {'color': 'white'}, 'notice': {'color': 'magenta'}, 'spam': {'color': 'green', 'faint': True}, 'success': {'bold': True, 'color': 'green'}, 'verbose': {'color': 'blue'}, 'warning': {'color': 'yellow'}}
    level = logging.DEBUG if '-v' in sys.argv or '--verbose' in sys.argv else logging.INFO
    if 1:
        coloredlogs.install(level=level, fmt='%(asctime)s.%(msecs)03d \033[0;90m%(levelname)-8s '
                            ''
                            '\033[0;36m%(filename)-18s%(lineno)3d\033[00m '
                            '%(message)s',
                            level_styles = styles)
    else:
        # Show process name
        coloredlogs.install(level=level, fmt='%(asctime)s.%(msecs)03d \033[0;90m%(levelname)-8s '
                                '\033[0;90m[\033[00m \033[0;35m%(processName)-15s\033[00m\033[0;90m]\033[00m '
                                '\033[0;36m%(filename)s:%(lineno)d\033[00m '
                                '%(message)s')

    logging.info("---[ Starting %s ]---------------------------------------------" % sys.argv[0])
    cameraDelay = args.camera_delay
    cameraMoveSpeed = args.camera_move_speed
    cameraElevationCorrection = args.camera_elevation_correction
    cameraBearingCorrection = args.camera_bearing_correction
    cameraZoom = args.camera_zoom
    camera_longitude = args.lon
    camera_latitude = args.lat
    camera_altitude = args.alt # Altitude is in METERS
    camera_lead = args.camera_lead
    
    if args.conn: 
        # Create the BlobServiceClient object which will be used to create a container client
        blob_service_client = BlobServiceClient.from_connection_string(args.conn)

    threading.Thread(target=moveCamera, args=[args.axis_ip, args.axis_username, args.axis_password],daemon=True).start()
        # Sleep for a bit so we're not hammering the HAT with updates
    time.sleep(0.005)
    flight_topic=args.mqtt_flight_topic
    object_topic = args.mqtt_object_topic
    print("connecting to MQTT broker at "+ args.mqtt_host+", channel '"+flight_topic+"'")
    client = mqtt.Client("skyscan-axis-ptz-camera-" + ID) #create new instance

    client.on_message=on_message #attach function to callback

    client.connect(args.mqtt_host) #connect to broker
    client.loop_start() #start the loop
    client.subscribe(flight_topic)
    client.subscribe(object_topic)
    client.subscribe(config_topic)
    client.subscribe("skyscan/egi")
    client.publish("skyscan/registration", "skyscan-axis-ptz-camera-"+ID+" Registration", 0, False)

    #############################################
    ##                Main Loop                ##
    #############################################
    timeHeartbeat = 0
    while True:
        if timeHeartbeat < time.mktime(time.gmtime()):
            timeHeartbeat = time.mktime(time.gmtime()) + 10
            client.publish("skyscan/heartbeat", "skyscan-axis-ptz-camera-"+ID+" Heartbeat", 0, False)
        time.sleep(0.1)



if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.critical(e, exc_info=True)
