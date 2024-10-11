FROM python:3.11-alpine
ARG group=jncep
ARG user=jncep
ARG home=/home/$user
RUN apk --no-cache add bash git jq curl
RUN pip install --upgrade pip
RUN pip install git+https://github.com/gvellut/jncep.git@$(curl -s https://api.github.com/repos/gvellut/jncep/releases/latest | jq -r '.tag_name')
ENTRYPOINT /usr/local/bin/jncep