"""daemon.py

Main Control and Orchestration Class for the Small Radio Telescope

"""

from time import sleep, time
from datetime import timedelta, datetime
from threading import Thread
from queue import Queue
from xmlrpc.client import ServerProxy
from pathlib import Path
from operator import add

import zmq
import json
import numpy as np

from .rotor_control.rotors import Rotor
from .radio_control.radio_task_starter import (
    RadioProcessTask,
    RadioSaveRawTask,
    RadioCalibrateTask,
    RadioSaveSpecRadTask,
)
from .utilities.object_tracker import EphemerisTracker
from .utilities.yaml_tools import validate_yaml_schema, load_yaml
from .utilities.functions import azel_within_range


class SmallRadioTelescopeDaemon:
    """
    Controller Class for the Small Radio Telescope
    """

    def __init__(self, config_directory):
        """Initializer for the Small Radio Telescope Daemon

        Parameters
        ----------
        config_directory : str
            Path to the Folder Containing the Configuration for the SRT
        """

        # Validate and Parse YAML Settings
        self.config_directory = config_directory
        validate_yaml_schema(path=config_directory)
        config_dict = load_yaml(path=config_directory)

        # Store Individual Settings In Object
        self.station = config_dict["STATION"]
        self.contact = config_dict["EMERGENCY_CONTACT"]
        self.az_limits = (
            config_dict["AZLIMITS"]["lower_bound"],
            config_dict["AZLIMITS"]["upper_bound"],
        )
        self.el_limits = (
            config_dict["ELLIMITS"]["lower_bound"],
            config_dict["ELLIMITS"]["upper_bound"],
        )
        self.stow_location = (
            config_dict["STOW_LOCATION"]["azimuth"],
            config_dict["STOW_LOCATION"]["elevation"],
        )
        self.cal_location = (
            config_dict["CAL_LOCATION"]["azimuth"],
            config_dict["CAL_LOCATION"]["elevation"],
        )
        self.motor_type = config_dict["MOTOR_TYPE"]
        self.motor_port = config_dict["MOTOR_PORT"]
        self.radio_center_frequency = config_dict["RADIO_CF"]
        self.radio_sample_frequency = config_dict["RADIO_SF"]
        self.radio_num_bins = config_dict["RADIO_NUM_BINS"]
        self.radio_integ_cycles = config_dict["RADIO_INTEG_CYCLES"]
        self.radio_autostart = config_dict["RADIO_AUTOSTART"]
        self.beamwidth = config_dict["BEAMWIDTH"]
        self.temp_sys = config_dict["TSYS"]
        self.temp_cal = config_dict["TCAL"]
        self.save_dir = config_dict["SAVE_DIRECTORY"]

        # Generate Default Calibration Values
        # Values are Set Up so that Uncalibrated and Calibrated Spectra are the Same Values
        # Unless there is a pre-exisiting calibration from a previous run
        self.cal_values = [1.0 for _ in range(self.radio_num_bins)]
        self.cal_power = 1.0 / (self.temp_sys + self.temp_cal)
        calibration_path = Path(self.config_directory, "calibration.json")
        if calibration_path.is_file():
            with open(calibration_path, "r") as input_file:
                try:
                    cal_data = json.load(input_file)
                    # If Calibration is of a Different Size Than The Current FFT Size, Discard
                    if len(cal_data["cal_values"]) == self.radio_num_bins:
                        self.cal_values = cal_data["cal_values"]
                        self.cal_power = cal_data["cal_pwr"]
                except KeyError:
                    pass

        # Create Helper Object Which Tracks Celestial Objects
        self.ephemeris_tracker = EphemerisTracker(
            self.station["latitude"],
            self.station["longitude"],
            config_file=str(Path(config_directory, "sky_coords.csv").absolute()),
        )
        self.ephemeris_locations = self.ephemeris_tracker.get_all_azimuth_elevation()
        self.ephemeris_cmd_location = None

        # Create Rotor Command Helper Object
        self.rotor = Rotor(
            self.motor_type, self.motor_port, self.az_limits, self.el_limits,
        )
        self.rotor_location = self.stow_location
        self.rotor_destination = self.stow_location
        self.rotor_offsets = (0.0, 0.0)
        self.rotor_cmd_location = tuple(
            map(add, self.rotor_destination, self.rotor_offsets)
        )

        # Create Radio Processing Task (Wrapper for GNU Radio Script)
        self.radio_process_task = RadioProcessTask(
            num_bins=self.radio_num_bins, num_integrations=self.radio_integ_cycles
        )
        self.radio_queue = Queue()
        self.radio_save_raw_task = None
        self.radio_save_spec_task = None

        # Create Object for Keeping Track of What Commands Are Running or Have Failed
        self.current_queue_item = "None"
        self.command_queue = Queue()
        self.command_error_logs = []

    def log_message(self, message):
        """Writes Contents to a Logging List and Prints

        Parameters
        ----------
        message : str
            Message to Log and Print

        Returns
        -------
        None
        """
        self.command_error_logs.append((time(), message))
        print(message)

    def n_point_scan(self, object_id):
        """Runs an N-Point (25) Scan About an Object

        Parameters
        ----------
        object_id : str
            Name of the Object to Perform N-Point Scan About

        Returns
        -------
        None
        """
        self.ephemeris_cmd_location = None
        self.radio_queue.put(("soutrack", object_id))
        for scan in range(25):
            new_rotor_destination = self.ephemeris_locations[object_id]
            i = (scan // 5) - 2
            j = (scan % 5) - 2
            el_dif = i * self.beamwidth * 0.5
            az_dif_scalar = np.cos((new_rotor_destination[1] + el_dif) * np.pi / 180.0)
            az_dif = j * self.beamwidth * 0.5 / az_dif_scalar
            new_rotor_offsets = (az_dif, el_dif)
            if self.rotor.angles_within_bounds(*new_rotor_destination):
                self.rotor_destination = new_rotor_destination
                self.point_at_offset(*new_rotor_offsets)
            sleep(5)
        self.rotor_offsets = (0.0, 0.0)
        self.ephemeris_cmd_location = object_id

    def beam_switch(self, object_id):
        """Swings Antenna Across Object
        
        Parameters
        ----------
        object_id : str
            Name of the Object to Perform Beam-Switch About

        Returns
        -------

        """
        self.ephemeris_cmd_location = None
        self.radio_queue.put(("soutrack", object_id))
        new_rotor_destination = self.ephemeris_locations[object_id]
        for j in range(0, 3):
            self.radio_queue.put(("beam_switch", j + 1))
            az_dif_scalar = np.cos(new_rotor_destination[1] * np.pi / 180.0)
            az_dif = (j % 3 - 1) * self.beamwidth / az_dif_scalar
            new_rotor_offsets = (az_dif, 0)
            if self.rotor.angles_within_bounds(*new_rotor_destination):
                self.rotor_destination = new_rotor_destination
                self.point_at_offset(*new_rotor_offsets)
            sleep(5)
        self.rotor_offsets = (0.0, 0.0)
        self.radio_queue.put(("beam_switch", 0))
        self.ephemeris_cmd_location = object_id

    def point_at_object(self, object_id):
        """Points Antenna Directly at Object, and Sets Up Tracking to Follow it

        Parameters
        ----------
        object_id : str
            Name of Object to Point at and Track

        Returns
        -------
        None
        """
        self.rotor_offsets = (0.0, 0.0)
        self.radio_queue.put(("soutrack", object_id))
        new_rotor_cmd_location = self.ephemeris_locations[object_id]
        if self.rotor.angles_within_bounds(*new_rotor_cmd_location):
            self.ephemeris_cmd_location = object_id
            self.rotor_destination = new_rotor_cmd_location
            self.rotor_cmd_location = new_rotor_cmd_location
            while not azel_within_range(self.rotor_location, self.rotor_cmd_location):
                sleep(0.1)
        else:
            self.log_message(f"Object {object_id} Not in Motor Bounds")
            self.ephemeris_cmd_location = None

    def point_at_azel(self, az, el):
        """Points Antenna at a Specific Azimuth and Elevation

        Parameters
        ----------
        az : float
            Azimuth, in degrees, to turn antenna towards
        el : float
            Elevation, in degrees, to point antenna upwards at

        Returns
        -------
        None
        """
        self.ephemeris_cmd_location = None
        self.rotor_offsets = (0.0, 0.0)
        self.radio_queue.put(("soutrack", f"azel_{az}_{el}"))
        new_rotor_destination = (az, el)
        new_rotor_cmd_location = new_rotor_destination
        if self.rotor.angles_within_bounds(*new_rotor_cmd_location):
            self.rotor_destination = new_rotor_destination
            self.rotor_cmd_location = new_rotor_cmd_location
            while not azel_within_range(self.rotor_location, self.rotor_cmd_location):
                sleep(0.1)
        else:
            self.log_message(f"Object at {new_rotor_cmd_location} Not in Motor Bounds")

    def point_at_offset(self, az_off, el_off):
        """From the Current Object or Position Pointed At, Move to an Offset of That Location

        Parameters
        ----------
        az_off : float
            Number of Degrees in Azimuth Offset
        el_off : float
            Number of Degrees in Elevation Offset

        Returns
        -------
        None
        """
        new_rotor_offsets = (az_off, el_off)
        new_rotor_cmd_location = tuple(
            map(add, self.rotor_destination, new_rotor_offsets)
        )
        if self.rotor.angles_within_bounds(*new_rotor_cmd_location):
            self.rotor_offsets = new_rotor_offsets
            self.rotor_cmd_location = new_rotor_cmd_location
            while not azel_within_range(self.rotor_location, self.rotor_cmd_location):
                sleep(0.1)
        else:
            self.log_message(f"Offset {new_rotor_offsets} Out of Bounds")

    def stow(self):
        """Moves the Antenna Back to Its Stow Location

        Returns
        -------
        None
        """
        self.ephemeris_cmd_location = None
        self.radio_queue.put(("soutrack", "at_stow"))
        self.rotor_offsets = (0.0, 0.0)
        self.rotor_destination = self.stow_location
        self.rotor_cmd_location = self.stow_location
        while not azel_within_range(self.rotor_location, self.rotor_cmd_location):
            sleep(0.1)

    def calibrate(self):
        """Runs Calibration Processing and Pushes New Values to Processing Script

        Returns
        -------
        None
        """
        radio_cal_task = RadioCalibrateTask(
            self.radio_num_bins, self.radio_integ_cycles, self.config_directory,
        )
        radio_cal_task.start()
        radio_cal_task.join(30)
        path = Path(self.config_directory, "calibration.json")
        with open(path, "r") as input_file:
            cal_data = json.load(input_file)
            self.cal_values = cal_data["cal_values"]
            self.cal_power = cal_data["cal_pwr"]
        self.radio_queue.put(("cal_pwr", self.cal_power))
        self.radio_queue.put(("cal_values", self.cal_values))
        self.log_message("Calibration Done")

    def start_recording(self, name):
        """Starts Recording Data

        Parameters
        ----------
        name : str
            Name of the File to Be Recorded

        Returns
        -------
        None
        """
        if self.radio_save_raw_task is None and self.radio_save_spec_task is None:
            if name is None or not name.endswith(".rad"):
                self.radio_save_raw_task = RadioSaveRawTask(
                    self.radio_sample_frequency, self.save_dir, name
                )
                self.radio_save_raw_task.start()
            else:
                name = None if name == "*.rad" else name
                self.radio_save_spec_task = RadioSaveSpecRadTask(
                    self.radio_sample_frequency,
                    self.radio_num_bins,
                    self.save_dir,
                    name,
                )
        else:
            self.log_message("Cannot Start Recording - Already Recording")

    def stop_recording(self):
        """Stops Any Current Recording, if Running

        Returns
        -------
        None
        """
        if self.radio_save_raw_task is not None:
            self.radio_save_raw_task.terminate()
            self.radio_save_raw_task = None
        if self.radio_save_spec_task is not None:
            self.radio_save_spec_task.terminate()
            self.radio_save_spec_task = None

    def set_freq(self, frequency):
        """Set the Frequency of the Processing Script

        Parameters
        ----------
        frequency : float
            Center Frequency, in Hz, to Set SDR to

        Returns
        -------
        None
        """
        self.radio_center_frequency = frequency  # Set Local Value
        self.radio_queue.put(
            ("freq", self.radio_center_frequency)
        )  # Push Update to GNU Radio

    def set_samp_rate(self, samp_rate):
        """Set the Sample Rate of the Processing Script

        Note that this stops any currently running raw saving tasks

        Parameters
        ----------
        samp_rate : float
            Sample Rate for the SDR in Hz

        Returns
        -------
        None
        """
        if self.radio_save_raw_task is not None:
            self.radio_save_raw_task.terminate()
        self.radio_sample_frequency = samp_rate
        self.radio_queue.put(("samp_rate", self.radio_sample_frequency))

    def quit(self):
        """Stops the Daemon Process

        Returns
        -------
        None
        """
        keep_running = False
        self.radio_queue.put(("is_running", keep_running))

    def update_ephemeris_location(self):
        """Periodically Updates Object Locations for Tracking Sky Objects

        Is Operated as an Infinite Looping Thread Function

        Returns
        -------
        None
        """
        last_updated_time = None
        while True:
            if last_updated_time is None or time() - last_updated_time > 10:
                last_updated_time = time()
                self.ephemeris_tracker.update_all_az_el()
            self.ephemeris_locations = (
                self.ephemeris_tracker.get_all_azimuth_elevation()
            )
            if self.ephemeris_cmd_location is not None:
                new_rotor_destination = self.ephemeris_locations[
                    self.ephemeris_cmd_location
                ]
                new_rotor_cmd_location = tuple(
                    map(add, new_rotor_destination, self.rotor_offsets)
                )
                if self.rotor.angles_within_bounds(
                    *new_rotor_destination
                ) and self.rotor.angles_within_bounds(*new_rotor_cmd_location):
                    self.rotor_destination = new_rotor_destination
                    self.rotor_cmd_location = new_rotor_cmd_location
                else:
                    self.log_message(
                        f"Object {self.ephemeris_cmd_location} moved out of motor bounds"
                    )
                    self.ephemeris_cmd_location = None
            sleep(1)

    def update_rotor_status(self):
        """Periodically Sets Rotor Azimuth and Elevation and Fetches New Antenna Position

        Is Operated as an Infinite Looping Thread Function

        Returns
        -------
        None
        """
        while True:
            try:
                current_rotor_cmd_location = self.rotor_cmd_location
                if not azel_within_range(
                    self.rotor_location, current_rotor_cmd_location
                ):
                    self.rotor.set_azimuth_elevation(*current_rotor_cmd_location)
                    sleep(1)
                    start_time = time()
                    while (
                        not azel_within_range(
                            self.rotor_location, current_rotor_cmd_location
                        )
                    ) and (time() - start_time) < 10:
                        self.rotor_location = self.rotor.get_azimuth_elevation()
                        self.radio_queue.put(
                            ("motor_az", float(self.rotor_location[0]))
                        )
                        self.radio_queue.put(
                            ("motor_el", float(self.rotor_location[1]))
                        )
                        sleep(0.5)
                else:
                    self.rotor_location = self.rotor.get_azimuth_elevation()
                    self.radio_queue.put(("motor_az", float(self.rotor_location[0])))
                    self.radio_queue.put(("motor_el", float(self.rotor_location[1])))
                    sleep(1)
            except AssertionError as e:
                self.log_message(str(e))
            except ValueError as e:
                self.log_message(str(e))

    def update_command_queue(self):
        """Waits for New Commands Coming in Over ZMQ PUSH/PULL

        Is Operated as an Infinite Looping Thread Function

        Returns
        -------
        None
        """
        context = zmq.Context()
        command_port = 5556
        command_socket = context.socket(zmq.PULL)
        command_socket.bind("tcp://*:%s" % command_port)
        while True:
            self.command_queue.put(command_socket.recv_string())

    def update_status(self):
        """Periodically Publishes Daemon Status for Dashboard (or any other subscriber)

        Is Operated as an Infinite Looping Thread Function

        Returns
        -------
        None
        """
        context = zmq.Context()
        status_port = 5555
        status_socket = context.socket(zmq.PUB)
        status_socket.bind("tcp://*:%s" % status_port)
        while True:
            status = {
                "beam_width": self.beamwidth,
                "location": self.station,
                "motor_azel": self.rotor_location,
                "motor_cmd_azel": self.rotor_cmd_location,
                "object_locs": self.ephemeris_locations,
                "az_limits": self.az_limits,
                "el_limits": self.el_limits,
                "center_frequency": self.radio_center_frequency,
                "bandwidth": self.radio_sample_frequency,
                "motor_offsets": self.rotor_offsets,
                "queued_item": self.current_queue_item,
                "queue_size": self.command_queue.qsize(),
                "emergency_contact": self.contact,
                "error_logs": self.command_error_logs,
                "temp_cal": self.temp_cal,
                "temp_sys": self.temp_sys,
                "cal_power": self.cal_power,
            }
            status_socket.send_json(status)
            sleep(0.5)

    def update_radio_settings(self):
        """Coordinates Sending XMLRPC Commands to the GNU Radio Script

        Is Operated as an Infinite Looping Thread Function

        Returns
        -------
        None
        """
        rpc_server = ServerProxy("http://localhost:5557/")
        while True:
            method, value = self.radio_queue.get()
            call = getattr(rpc_server, f"set_{method}")
            call(value)
            sleep(0.1)

    def srt_daemon_main(self):
        """Starts and Processes Commands for the SRT

        Returns
        -------
        None
        """

        # Create Infinite Looping Threads
        ephemeris_tracker_thread = Thread(
            target=self.update_ephemeris_location, daemon=True
        )
        rotor_pointing_thread = Thread(target=self.update_rotor_status, daemon=True)
        command_queueing_thread = Thread(target=self.update_command_queue, daemon=True)
        status_thread = Thread(target=self.update_status, daemon=True)
        radio_thread = Thread(target=self.update_radio_settings, daemon=True)

        # If the GNU Radio Script Should be Running, Start It
        if self.radio_autostart:
            try:
                self.radio_process_task.start()
            except RuntimeError as e:
                self.log_message(str(e))
            sleep(5)

        # Send Settings to the GNU Radio Script
        radio_params = {
            "Frequency": ("freq", self.radio_center_frequency),
            "Sample Rate": ("samp_rate", self.radio_sample_frequency),
            "Motor Azimuth": ("motor_az", self.rotor_location[0]),
            "Motor Elevation": ("motor_el", self.rotor_location[1]),
            "Object Tracking": ("soutrack", "at_stow"),
            "System Temp": ("tsys", self.temp_sys),
            "Calibration Temp": ("tcal", self.temp_cal),
            "Calibration Power": ("cal_pwr", self.cal_power),
            "Calibration Values": ("cal_values", self.cal_values),
            "Is Running": ("is_running", True),
        }
        for name in radio_params:
            self.log_message(f"Setting {name}")
            self.radio_queue.put(radio_params[name])

        # Start Infinite Looping Update Threads
        ephemeris_tracker_thread.start()
        rotor_pointing_thread.start()
        command_queueing_thread.start()
        status_thread.start()
        radio_thread.start()

        keep_running = True

        while keep_running:
            try:
                # Await Command for the SRT
                self.current_queue_item = "None"
                command = self.command_queue.get()
                self.log_message(f"Running Command '{command}'")
                self.current_queue_item = command
                if len(command) < 2 or command[0] == "*":
                    continue
                elif command[0] == ":":
                    command = command[1:].strip()
                command_parts = command.split(" ")
                command_name = command_parts[0].lower()

                # If Command Starts With a Valid Object Name
                if command_parts[0] in self.ephemeris_locations:
                    if command_parts[-1] == "n":  # N-Point Scan About Object
                        self.n_point_scan(object_id=command_parts[0])
                    elif command_parts[-1] == "b":  # Beam-Switch Away From Object
                        self.beam_switch(object_id=command_parts[0])
                    else:  # Point Directly At Object
                        self.point_at_object(object_id=command_parts[0])
                elif command_name == "stow":
                    self.stow()
                elif command_name == "cal":
                    self.point_at_azel(*self.cal_location)
                elif command_name == "calibrate":
                    self.calibrate()
                elif command_name == "quit":
                    self.quit()
                elif command_name == "record":
                    self.start_recording(
                        name=(None if len(command_parts) <= 1 else command_parts[1])
                    )
                elif command_name == "roff":
                    self.stop_recording()
                elif command_name == "freq":
                    self.set_freq(frequency=float(command_parts[1]) * pow(10, 6))
                elif command_name == "samp":
                    self.set_samp_rate(samp_rate=float(command_parts[1]) * pow(10, 6))
                elif command_name == "azel":
                    self.point_at_azel(
                        float(command_parts[1]), float(command_parts[2]),
                    )
                elif command_name == "offset":
                    self.point_at_offset(
                        float(command_parts[1]), float(command_parts[2])
                    )
                elif command_name.isnumeric():  # If Command is a Number, Sleep that Long
                    sleep(float(command_name))
                elif command_name == "wait":
                    sleep(float(command_parts[1]))
                elif command_name.split(":")[0] == "lst":  # Wait Until Next Time H:M:S
                    time_string = command_name.replace("LST:", "")
                    time_val = datetime.strptime(time_string, "%H:%M:%S")
                    while time_val < datetime.utcfromtimestamp(time()):
                        time_val += timedelta(days=1)
                    time_delta = (
                        time_val - datetime.utcfromtimestamp(time())
                    ).total_seconds()
                    sleep(time_delta)
                elif len(command_name.split(":")) == 5:  # Wait Until Y:D:H:M:S
                    time_val = datetime.strptime(command_name, "%Y:%j:%H:%M:%S")
                    time_delta = (
                        time_val - datetime.utcfromtimestamp(time())
                    ).total_seconds()
                    sleep(time_delta)
                else:
                    self.log_message(f"Command Not Identified '{command}'")

            except IndexError as e:
                self.log_message(str(e))
            except ValueError as e:
                self.log_message(str(e))
            except ConnectionRefusedError as e:
                self.log_message(str(e))

        # On End, Return to Stow and End Recordings
        self.stow()
        self.stop_recording()
        if self.radio_autostart:
            self.radio_process_task.terminate()


if __name__ == "__main__":
    daemon = SmallRadioTelescopeDaemon(config_directory="./config/")
    daemon.srt_daemon_main()