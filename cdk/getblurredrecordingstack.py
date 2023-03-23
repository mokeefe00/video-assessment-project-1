from aws_cdk import core as cdk
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_iam as _iam
import aws_cdk.aws_lambda as lambda_
from aws_cdk.aws_lambda_event_sources import S3EventSource
import aws_cdk.aws_stepfunctions as sfn
import aws_cdk.aws_stepfunctions_tasks as tasks

class RecordWithFaceBlurStack(cdk.Stack):

    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ###############################################################################################
                                                #S3#
        ###############################################################################################

        ## S3 buckets for input and output locations
        clipInputBucket = s3.Bucket(self, "clipfragments")
        recordingNotBlurredBucket = s3.Bucket(self, "recordings-notblurred")
        recordingBlurredBucket = s3.Bucket(self, "recordings-blurred")


        ###############################################################################################
                                                #Lambda#
        ###############################################################################################

        ## Set up two lambda functions for calling getclip from KVS and also mediaconvert to stitch clips together into recording
        #get clip and put to 1st bucket
        getClipFromKVS = lambda_.Function(self, "getClips-KVS", 
            timeout=cdk.Duration.seconds(600), 
            memory_size=1024,
            code=lambda_.Code.from_asset('./lambdas/getclip.js'),
            handler='lambda_function.lambda_handler',
            runtime=lambda_.Runtime.NODEJS_16_X
        )

        getClipFromKVS.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[
                clipInputBucket.bucket_arn,
                '{}/*'.format(clipInputBucket.bucket_arn)  
            ]
        ))

        #start mediaconvert and store unblurred recording into 2nd bucket
        mp4stitch = lambda_.Function(self, "mp4stitch", timeout=cdk.Duration.seconds(600), memory_size=512,
            code=lambda_.Code.from_asset('./lambdas/mp4stitch.js'),
            handler='lambda_function.lambda_handler',
            runtime=lambda_.Runtime.NODEJS_16_X
        )

        mp4stitch.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[
                clipInputBucket.bucket_arn,
                '{}/*'.format(clipInputBucket.bucket_arn),
                recordingNotBlurredBucket.bucket_arn,
                '{}/*'.format(recordingNotBlurredBucket.bucket_arn)
            ]
        ))

        mp4stitch.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["mediaconvert:CreateJob"],
            resources=["*"]
        ))

        ## Lambda triggering the Rekognition job and the StepFunctions workflow
        startFaceDetect = lambda_.Function(self, "startFaceDetect", timeout=cdk.Duration.seconds(600), memory_size=512,
            code=lambda_.Code.from_asset('./lambdas/startfacedetect.py'),
            handler="lambda_function.lambda_handler",
            runtime=lambda_.Runtime.PYTHON_3_7
        )

        #Adding S3 event sources triggers for the startFaceDetectFunction, allowing .mp4 files only
        # startFaceDetectFunction.add_event_source(S3EventSource(clipInputBucket,
        #     events=[s3.EventType.OBJECT_CREATED],
        #     filters=[s3.NotificationKeyFilter(suffix='.mov')]))
        startFaceDetect.add_event_source(S3EventSource(clipInputBucket,
            events=[s3.EventType.OBJECT_CREATED],
            filters=[s3.NotificationKeyFilter(suffix='.mp4')]))

        #Allowing startFaceDetect to access the S3 input bucket
        startFaceDetect.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[
                recordingNotBlurredBucket.bucket_arn,
                '{}/*'.format(recordingNotBlurredBucket.bucket_arn)]))

        #Allowing startFaceDetect to call Rekognition
        startFaceDetect.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["rekognition:StartFaceDetection"],
            resources=["*"]))

        ## Lambda checking Rekognition job status 
        checkJobStatus = lambda_.Function(self, "checkJobStatus", timeout=cdk.Duration.seconds(600), memory_size=512,
            code=lambda_.Code.from_asset('./lambdas/checkJobStatus.py'),
            handler="lambda_function.lambda_handler",
            runtime=lambda_.Runtime.PYTHON_3_7)

        #Allowing checkJobStatus to call Rekognition
        checkJobStatus.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["rekognition:GetFaceDetection"],
            resources=["*"]))

        ## Lambda getting data from Rekognition
        getFacesInfo = lambda_.Function(self, "getFacesInfo", timeout=cdk.Duration.seconds(600), memory_size=512,
            code=lambda_.Code.from_asset('./lambdas/getfacesinfo.py'),
            handler="lambda_function.lambda_handler",
            runtime=lambda_.Runtime.PYTHON_3_7)

        #Allowing getFacesInfo to call Rekognition
        getFacesInfo.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["rekognition:GetFaceDetection"],
            resources=["*"]))
        
        ## Lambda blurring the faces on the video based on Rekognition data
        blurFaces = lambda_.DockerImageFunction(self, "blurFaces", timeout=cdk.Duration.seconds(600), memory_size=2048,
            code=lambda_.DockerImageCode.from_image_asset("./lambdas/blurfaces-dockersetup"))

        #Adding the S3 output bucket name as an ENV variable to the blurFaces 
        blurFaces.add_environment(key="OUTPUT_BUCKET", value=recordingBlurredBucket.bucket_name)

        #Allowing blurFaces to access the S3 input and output buckets
        blurFaces.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[
                recordingNotBlurredBucket.bucket_arn,
                recordingBlurredBucket.bucket_arn,
                '{}/*'.format(recordingNotBlurredBucket.bucket_arn),
                '{}/*'.format(recordingBlurredBucket.bucket_arn)]))
        

        ###############################################################################################
                                                #StepFunctions#
        ###############################################################################################

        ## State for waiting 1 second
        wait_1 = sfn.Wait(self, "Wait 1 Second",
            time=sfn.WaitTime.duration(cdk.Duration.seconds(1))
        )

        ## State in case of execution failure
        job_failed = sfn.Fail(self, "Execution Failed",
            cause="Face Detection Failed",
            error="Could not get job_status = 'SUCCEEDED'"
        )

        ## State in case of execution success
        job_succeeded = sfn.Succeed(self, "Execution Succeeded")

        ## Task checking the Rekognition job status
        update_job_status = tasks.LambdaInvoke(self, "Check Job Status",
            lambda_function=checkJobStatus,
            # Lambda's result is in the attribute `Payload`
            input_path="$.body",
            output_path="$.Payload"
        )

        ## Task getting the data from Rekognition once the update_job_status task is a success
        get_timestamps_and_faces = tasks.LambdaInvoke(self, "Get Timestamps and Faces",
            lambda_function=getFacesInfo,
            input_path="$.body",
            output_path="$.Payload"
        )

        ## Task blurring the faces appearing on the video based on the get_timestamps_and_faces data
        blur_faces = tasks.LambdaInvoke(self, "Blur Faces on Video",
            lambda_function=blurFaces,
            input_path="$.body",
            output_path="$.Payload"
        )

        ## Defining a choice
        choice = sfn.Choice(self, "Job finished?")

        #Adding conditions with .when()
        choice.when(sfn.Condition.string_equals("$.body.job_status", "IN_PROGRESS"), wait_1.next(update_job_status))
        choice.when(sfn.Condition.string_equals("$.body.job_status", "SUCCEEDED"), get_timestamps_and_faces.next(blur_faces).next(job_succeeded))
        #Adding a default choice with .otherwise() if none of the above choices are matched
        choice.otherwise(job_failed)

        ## Definition of the State Machine
        definition = update_job_status.next(choice)

        ## Actuel State Machine built with the above definition
        stateMachine = sfn.StateMachine(self, "StateMachine",
            definition=definition,
            timeout=cdk.Duration.minutes(15)
        )

        ## Adding the State Machine ARN to the ENV variables of the Lambda startFaceDetectFunction
        startFaceDetect.add_environment(key="STATE_MACHINE_ARN", value=stateMachine.state_machine_arn)

        # Allowing startFaceDetectFunction to start the StepFunctions workflow
        startFaceDetect.add_to_role_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            actions=["states:StartExecution"],
            resources=[
                stateMachine.state_machine_arn,
                '{}/*'.format(stateMachine.state_machine_arn)]))

        
