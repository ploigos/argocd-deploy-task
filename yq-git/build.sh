BUILD_DIR=./build/

[ -d ${BUILD_DIR} ] && rm -rf ${BUILD_DIR}
mkdir ${BUILD_DIR}

sed 's/^/        /' task.py > $BUILD_DIR/task-wip.py          # Indent the python script
head -n -1 task-template.yml > $BUILD_DIR/task-wip.yml
cat ${BUILD_DIR}/task-wip.yml $BUILD_DIR/task-wip.py > $BUILD_DIR/task.yml   # Add the python script to the template

