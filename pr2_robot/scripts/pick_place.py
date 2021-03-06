#!/usr/bin/env python

# Import modules
import numpy as np
import sklearn
from sklearn.preprocessing import LabelEncoder
import pickle
from sensor_stick.srv import GetNormals
from sensor_stick.features import compute_color_histograms
from sensor_stick.features import compute_normal_histograms
from visualization_msgs.msg import Marker
from sensor_stick.marker_tools import *
from sensor_stick.msg import DetectedObjectsArray
from sensor_stick.msg import DetectedObject
from sensor_stick.pcl_helper import *

import rospy
import tf
from geometry_msgs.msg import Pose, Point, Quaternion
from std_msgs.msg import Float64
from std_msgs.msg import Int32
from std_msgs.msg import String
from pr2_robot.srv import *
from rospy_message_converter import message_converter
import yaml
import os

# Helper function to get surface normals
def get_normals(cloud):
    get_normals_prox = rospy.ServiceProxy('/feature_extractor/get_normals', GetNormals)
    return get_normals_prox(cloud).cluster

# Helper function to create a yaml friendly dictionary from ROS messages
def make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose):
    yaml_dict = {}
    yaml_dict["test_scene_num"] = test_scene_num.data
    yaml_dict["arm_name"]  = arm_name.data
    yaml_dict["object_name"] = object_name.data
    yaml_dict["pick_pose"] = message_converter.convert_ros_message_to_dictionary(pick_pose)
    yaml_dict["place_pose"] = message_converter.convert_ros_message_to_dictionary(place_pose)
    return yaml_dict

# Helper function to output to yaml file
def send_to_yaml(yaml_filename, dict_list):
    data_dict = {"object_list": dict_list}
    with open(yaml_filename, 'w') as outfile:
        yaml.dump(data_dict, outfile, default_flow_style=False)

# Define functions as required
def vox_filt( cloud, LEAF_SIZE = 0.005 ):
    vox = cloud.make_voxel_grid_filter()
    vox.set_leaf_size(LEAF_SIZE, LEAF_SIZE, LEAF_SIZE)
    return vox.filter()

def passthrough_filt( cloud, filter_axis = 'z', axis_min = 0.6, axis_max = 1.1 ):
    passthrough = cloud.make_passthrough_filter()
    passthrough.set_filter_field_name(filter_axis)
    passthrough.set_filter_limits(axis_min, axis_max)
    return passthrough.filter()

def outlier_filt( cloud, mean_k = 10, dev_mul = 0.01 ):
    out = cloud.make_statistical_outlier_filter()
    out.set_mean_k( mean_k )
    out.set_std_dev_mul_thresh( dev_mul)
    return out.filter()

def seg_plane( cloud, max_distance = 0.01 ):
    seg = cloud.make_segmenter()
    seg.set_model_type(pcl.SACMODEL_PLANE)
    seg.set_method_type(pcl.SAC_RANSAC)
    seg.set_distance_threshold(max_distance)
    inliers, coefficients = seg.segment()
    return inliers, coefficients

def euclidean_cluster( white_cloud, tolerance = 0.05, min = 100, max = 2500 ):
    tree = white_cloud.make_kdtree()
    # Create a cluster extraction object
    ec = white_cloud.make_EuclideanClusterExtraction()
    # Set tolerances for distance threshold
    # as well as minimum and maximum cluster size (in points)
    ec.set_ClusterTolerance( tolerance )
    ec.set_MinClusterSize( min )
    ec.set_MaxClusterSize( max )
    # Search the k-d tree for clusters
    ec.set_SearchMethod( tree )
    # Extract indices for each of the discovered clusters
    return ec.Extract()

# Callback function for your Point Cloud Subscriber
def pcl_callback(pcl_msg):

    # Convert ROS msg to PCL data
    cloud = ros_to_pcl( pcl_msg )

    # Outliers removing
    cloud = outlier_filt( cloud )

    # Voxel Grid Downsampling
    cloud = vox_filt( cloud )

    # PassThrough Filter
    cloud = passthrough_filt( cloud )
    cloud = passthrough_filt( cloud, filter_axis = 'y', axis_min = - 0.45, axis_max = 0.45 )

    # RANSAC Plane Segmentation
    inliers, coefficients = seg_plane( cloud )

    # Extract inliers and outliers
    cloud_table = cloud.extract( inliers, negative = False )
    cloud_objects = cloud.extract( inliers, negative = True )

    # Euclidean Clustering
    white_cloud = XYZRGB_to_XYZ( cloud_objects )
    cluster_indices = euclidean_cluster( white_cloud )

    # Create Cluster-Mask Point Cloud to visualize each cluster separately
    #Assign a color corresponding to each segmented object in scene
    cluster_color = get_color_list( len( cluster_indices ) )

    color_cluster_point_list = []

    for j, indices in enumerate( cluster_indices ):
        for i, indice in enumerate(indices):
            color_cluster_point_list.append( [ white_cloud[ indice ][ 0 ],
                                               white_cloud[ indice ][ 1 ],
                                               white_cloud[ indice ][ 2 ],
                                               rgb_to_float( cluster_color[ j ] ) ] )

    #Create new cloud containing all clusters, each with unique color
    cluster_cloud = pcl.PointCloud_PointXYZRGB()
    cluster_cloud.from_list( color_cluster_point_list )

    ros_cluster_cloud = pcl_to_ros( cluster_cloud )

    # Convert PCL data to ROS messages
    ros_cloud_table = pcl_to_ros( cloud_table )
    ros_cloud_objects = pcl_to_ros( cloud_objects )

    # Publish ROS messages
    pcl_table_pub.publish( ros_cloud_table )
    pcl_objects_pub.publish( ros_cloud_objects )

    pcl_cluster_pub.publish( ros_cluster_cloud )

    # Exercise-3 TODOs:

    # Classify the clusters! (loop through each detected cluster one at a time)
    detected_objects = []
    detected_objects_labels = []

    for idx, pts_list in enumerate( cluster_indices ):
        # Grab the points for the cluster
        pcl_cluster = cloud_objects.extract( pts_list )
        ros_cluster = pcl_to_ros( pcl_cluster )

        # Compute the associated feature vector
        chists = compute_color_histograms( ros_cluster, using_hsv = True )
        # normals = get_normals( ros_cluster )
        # nhists = compute_normal_histograms( normals )
        feature = chists # np.concatenate( ( chists, nhists ) )

        # Make the prediction
        prediction = clf.predict( scaler.transform( feature.reshape( 1, -1 ) ) )
        label = encoder.inverse_transform( prediction )[ 0 ]
        detected_objects_labels.append( label )

        # Publish a label into RViz
        label_pos = list( white_cloud[ pts_list[ 0 ] ] )
        label_pos[ 2 ] += .4
        object_markers_pub.publish( make_label( label, label_pos, idx ) )

        # Add the detected object to the list of detected objects.
        do = DetectedObject()
        do.label = label
        do.cloud = ros_cluster
        detected_objects.append( do )

    rospy.loginfo( 'Detected {} objects: {}'.format( len( detected_objects_labels ), detected_objects_labels ) )

    # Publish the list of detected objects
    detected_objects_pub.publish( detected_objects )

    # output yaml
    output_file = 'output_1.yaml'

    if not os.path.exists( output_file ):
        object_list_param = rospy.get_param( '/object_list' )
        dropbox_param = rospy.get_param( '/dropbox' )

        if dropbox_param[ 0 ][ 'group' ] == 'red':
            dropbox = {
                'red': dropbox_param[ 0 ],
                'green': dropbox_param[ 1 ]
            }
        else:
            dropbox = {
                'red': dropbox_param[ 1 ],
                'green': dropbox_param[ 0 ]
            }

        dicts = []
        for obj in object_list_param:
            name = obj[ 'name' ]
            group = obj[ 'group' ]
            box = dropbox[ group ]

            for do in detected_objects:
                if do.label == name:
                    ps = ros_to_pcl( do.cloud ).to_array()
                    cs = np.mean( ps, axis = 0 )[ : 3 ]

                    dict = make_yaml_dict( * pick_req( 1, box[ 'name' ], name, cs, box[ 'position' ] ) )
                    dicts.append( dict )

        send_to_yaml( output_file, dicts )

def pick_req( test_scene_num, arm_name, object_name,  pick_pose, place_pose ):
    msg_scene = Int32()
    msg_scene.data = test_scene_num

    msg_arm_name = String()
    msg_arm_name.data = arm_name

    msg_obj_name = String()
    msg_obj_name.data = object_name

    msg_pick_pose = Pose()
    msg_pick_pose.position = Point()
    msg_pick_pose.position.x = np.asscalar( pick_pose[ 0 ] )
    msg_pick_pose.position.y = np.asscalar( pick_pose[ 1 ] )
    msg_pick_pose.position.z = np.asscalar( pick_pose[ 2 ] )

    msg_place_pose = Pose()
    msg_place_pose.position = Point()
    msg_place_pose.position.x = place_pose[ 0 ]
    msg_place_pose.position.y = place_pose[ 1 ]
    msg_place_pose.position.z = place_pose[ 2 ]

    return msg_scene, msg_arm_name, msg_obj_name, msg_pick_pose, msg_place_pose

if __name__ == '__main__':

    # TODO: ROS node initialization
    rospy.init_node( 'pick_place', anonymous = True )

    # TODO: Create Subscribers
    pcl_sub = rospy.Subscriber( "/pr2/world/points", PointCloud2, pcl_callback, queue_size = 1 )

    # TODO: Create Publishers
    pcl_table_pub = rospy.Publisher( "/pcl_table", PointCloud2, queue_size = 1 )
    pcl_objects_pub = rospy.Publisher( "/pcl_objects", PointCloud2, queue_size = 1 )

    pcl_cluster_pub = rospy.Publisher( "/pcl_cluster", PointCloud2, queue_size = 1 )

    object_markers_pub = rospy.Publisher( '/object_markers', Marker, queue_size = 1 )
    detected_objects_pub = rospy.Publisher( '/detected_objects', DetectedObjectsArray, queue_size = 1 )

    # TODO: Load Model From disk
    model = pickle.load( open( 'model.sav', 'rb' ) )
    clf = model[ 'classifier' ]
    encoder = LabelEncoder()
    encoder.classes_ = model[ 'classes' ]
    scaler = model[ 'scaler' ]

    # Initialize color_list
    get_color_list.color_list = []

    # TODO: Spin while node is not shutdown
    while not rospy.is_shutdown():
        rospy.spin()
