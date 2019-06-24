package com.javaproject.demo.service;

import com.javaproject.demo.mapper.UserMapper;
import com.javaproject.demo.model.User;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

@Service
public class UserService {
    @Autowired
    private UserMapper userMapper;

    public void createOrUpdate(User user) {
        System.out.println(user.getAccountId());
        User dbUser = userMapper.findByUserId(user.getAccountId());

        if (dbUser == null){
            //插入一个新的用户
            user.setGmtCreate(System.currentTimeMillis());
            user.setGmtModified(user.getGmtCreate());
            userMapper.insert(user);
        }else{
            dbUser.setGmtModified(System.currentTimeMillis());
            //dbUser.setAvatarUrl(user.getAvatarUrl());
            dbUser.setName(user.getName());
            dbUser.setToken(user.getToken());
            userMapper.update(dbUser);
            //更新用户
        }
    }
}
