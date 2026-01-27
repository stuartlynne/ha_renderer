


#FROM python:3
#FROM frolvlad/alpine-python3:latest
#FROM python:3.10.0-alpine
#FROM python:3.12-alpine

FROM python:3.12-alpine

RUN apk update && apk upgrade
# Install required build dependencies
RUN apk add --no-cache --virtual .build-deps \
    build-base \
    linux-headers \
    libffi-dev \
    gcc \
    musl-dev \
    python3-dev \
    && pip install psutil \
    && apk del .build-deps

# update and get extra utilities
#RUN apt-get update && apt-get install -y vim less psmisc netcat-openbsd telnet libsnmp-dev pdftk ghostscript dos2unix
RUN apk add --no-cache bash vim git less busybox-extras netcat-openbsd net-snmp-dev make poppler-utils
RUN apk add --virtual build-deps gcc python3-dev musl-dev 
RUN apk add jpeg-dev zlib-dev libjpeg 

# add missing Helvetica font
RUN apk --update --upgrade --no-cache add fontconfig ttf-freefont ttf-dejavu font-noto font-noto-emoji terminus-font noto-fonts-extra \
	&& fc-cache -f \ 
	&& fc-list | sort 


# install openssh and generate keys
RUN apk add openssh
RUN ssh-keygen -A


ENV TIME_ZONE=America/Vancouver

# Set out hostname for avahi
#RUN echo "qllabels.local" > /etc/hostname && 
#mkdir -p /docker-entrypoint-init.d/ 

RUN adduser -D racedb
RUN passwd -d racedb

#RUN apk add py3-flask 
#RUN apk add --no-cache py3-requests 
#RUN apk add --no-cache py3-websockets
RUN apk add --no-cache jpeg-dev zlib-dev freetype-dev lcms2-dev openjpeg-dev tiff-dev tk-dev tcl-dev harfbuzz-dev fribidi-dev


RUN python3 -m pip install --upgrade pip
RUN python3 -m pip install setuptools 
RUN python3 -m pip install requests 
RUN python3 -m pip install websockets 
RUN python3 -m pip install jinja2 
RUN python3 -m pip install cairosvg 
RUN python3 -m pip install pillow 
RUN python3 -m pip install paho-mqtt 
RUN python3 -m pip install pillow --root-user-action=ignore

RUN cd / && git clone https://github.com/stuartlynne/ha_renderer.git 
RUN cd /ha_renderer && make install 

#RUN chmod +x /ha_renderer/ha_renderer/ha_renderer.sh
ENTRYPOINT ["tail", "-f", "/dev/null"]
#ENTRYPOINT ["/entry/ha_renderer.sh"]
